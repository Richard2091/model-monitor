# -*- coding: utf-8 -*-
"""邮件告警模块：模型状态变化（正常/异常）时合并发送一封告警邮件。

规则：仅在状态变化时发送；一轮内的所有变化合并为一封；发送失败不更新状态，
下一轮自动重试；每日发送次数有上限。命令行 `python alerts.py --test` 可发送测试邮件。
"""

import logging
import sys

import config
import database

logger = logging.getLogger(__name__)

# 邮件中错误信息的最大长度
_MESSAGE_MAX_LEN = 200


def _recipients():
    """解析收件人列表。

    @return 收件人邮箱字符串列表（已去除空白与空项）
    """
    # 按逗号拆分并清理空白
    return [x.strip() for x in str(config.ALERT_EMAIL_TO or "").split(",") if x.strip()]


def is_enabled():
    """判断告警是否已启用（需同时配置收件人与 SMTP 主机）。

    @return 是否启用
    """
    # 收件人或 SMTP 主机缺失即视为关闭
    return bool(_recipients() and config.SMTP_HOST)


def _display_names(targets):
    """从模型配置快照中提取模型显示名映射。

    @param targets 本轮探测目标（带 model_key、display_name 属性）
    @return 字典：模型ID -> 显示名（无显示名的不在字典中）
    """
    # 只收录非空显示名
    names = {}
    for target in targets or ():
        name = getattr(target, "display_name", None)
        if name:
            names[target.model_key] = name
    return names


def _collect_changes(results, states):
    """对比上次状态，找出本轮的状态变化与需静默记录的首次正常模型。

    @param results 本轮探测结果列表
    @param states 上次状态字典：模型ID -> 是否正常
    @return (changes, silent)：changes 为需告警的结果列表（附 recovered 标记），
            silent 为首次出现且正常、只需记录状态的模型ID列表
    """
    changes = []
    silent = []
    for item in results:
        model = item["model"]
        ok = bool(item["ok"])
        prev = states.get(model)
        if prev is None:
            # 无历史：红色算故障，正常只记录
            if ok:
                silent.append(model)
            else:
                changes.append({**item, "recovered": False})
        elif prev != ok:
            # 状态翻转：正常变红为故障，红变正常为恢复
            changes.append({**item, "recovered": ok})
    return changes, silent


def build_message(changes, names, now):
    """生成告警邮件的主题与纯文本正文。

    @param changes 状态变化列表，元素含 model、recovered、status_code、latency_ms、message
    @param names 模型显示名映射
    @param now 当前本地时间（datetime）
    @return (subject, body)
    """
    faults = sum(1 for c in changes if not c["recovered"])
    recoveries = len(changes) - faults
    # 主题按存在的类别拼接
    parts = []
    if faults:
        parts.append(f"{faults} 个模型异常")
    if recoveries:
        parts.append(f"{recoveries} 个恢复")
    subject = "【模型监控】" + "，".join(parts)
    # 正文：时间 + 每个模型一段
    lines = [f"时间：{now.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    for c in changes:
        model = c["model"]
        shown = names.get(model)
        title = f"{shown}（{model}）" if shown else model
        latency = c.get("latency_ms")
        message = str(c.get("message") or "")[:_MESSAGE_MAX_LEN]
        lines.append(f"模型：{title}")
        lines.append(f"状态：{'恢复' if c['recovered'] else '异常'}")
        lines.append(f"HTTP 状态码：{c.get('status_code')}")
        lines.append(f"延迟：{str(latency) + ' ms' if latency is not None else '-'}")
        lines.append(f"错误信息：{message or '-'}")
        lines.append("")
    # 配置了监控页链接时附在末尾
    if config.ALERT_PAGE_URL:
        lines.append(f"监控页：{config.ALERT_PAGE_URL}")
    return subject, "\n".join(lines).rstrip() + "\n"


def send_mail(subject, body):
    """通过 SMTP 发送一封纯文本邮件，失败时抛出异常。

    @param subject 邮件主题
    @param body 邮件正文
    @return None
    """
    # 懒加载，平时不占内存
    import smtplib
    from email.header import Header
    from email.message import EmailMessage
    from email.utils import formataddr

    from_addr = config.SMTP_FROM or config.SMTP_USERNAME
    msg = EmailMessage()
    msg["Subject"] = subject
    # 发件人显示名含中文，需 Header 编码
    msg["From"] = formataddr((Header(config.SMTP_FROM_NAME, "utf-8").encode(), from_addr))
    msg["To"] = ", ".join(_recipients())
    msg.set_content(body, charset="utf-8")
    # 按配置选择 SSL 直连或 STARTTLS
    if config.SMTP_USE_SSL:
        server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=config.SMTP_TIMEOUT)
    else:
        server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=config.SMTP_TIMEOUT)
    try:
        if not config.SMTP_USE_SSL:
            server.starttls()
        if config.SMTP_USERNAME:
            server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
        server.send_message(msg, from_addr=from_addr, to_addrs=_recipients())
    finally:
        server.close()


def _process_round(results, targets):
    """告警主流程（不含兜底）：判定变化、限流、发信、更新状态。

    @param results 本轮探测结果列表
    @param targets 本轮探测目标
    @return None
    """
    now = config.now_local()
    now_iso = now.isoformat()
    states = database.get_alert_states()
    changes, silent = _collect_changes(results, states)
    # 首次出现且正常的模型只记录状态
    if silent:
        database.save_alert_states([(m, True, now_iso) for m in silent])
    if not changes:
        return
    new_states = [(c["model"], bool(c["ok"]), now_iso) for c in changes]
    # 告警关闭：只更新状态，避免以后开启时把旧故障当新故障
    if not is_enabled():
        database.save_alert_states(new_states)
        return
    # 达到每日上限：不发信，但照样更新状态，免得第二天补发旧变化
    sent_today = database.count_alert_emails(now.strftime("%Y-%m-%d"))
    if sent_today >= config.ALERT_DAILY_LIMIT:
        logger.warning("今日告警邮件已达上限 %s，跳过 %d 条状态变化", config.ALERT_DAILY_LIMIT, len(changes))
        database.save_alert_states(new_states)
        return
    subject, body = build_message(changes, _display_names(targets), now)
    try:
        send_mail(subject, body)
    except Exception as exc:
        # 发送失败：记录失败，不写状态，下一轮仍为变化会自动重试
        logger.error("告警邮件发送失败: %s", exc)
        database.record_alert_email(subject, False, str(exc)[:500])
        return
    # 发送成功后才落状态
    database.record_alert_email(subject, True)
    database.save_alert_states(new_states)


def process_round(results, targets):
    """处理一轮探测结果的状态变化告警；任何异常只记日志，绝不影响探测。

    @param results 本轮探测结果列表
    @param targets 本轮探测目标（用于取显示名）
    @return None
    """
    try:
        _process_round(results, targets)
    except Exception:
        logger.exception("处理告警异常")


def main(argv):
    """命令行入口：`--test` 按当前配置发送测试邮件。

    @param argv 命令行参数列表（不含程序名）
    @return 进程退出码，成功 0，失败非 0
    """
    if "--test" not in argv:
        print("用法: python alerts.py --test")
        return 2
    if not is_enabled():
        print("失败: 未配置 ALERT_EMAIL_TO 或 SMTP_HOST")
        return 1
    # 发送测试邮件并打印结果
    try:
        send_mail("【模型监控】测试邮件", "这是一封测试邮件，收到说明邮件告警配置正确。\n")
    except Exception as exc:
        print(f"失败: {exc}")
        return 1
    print("成功: 测试邮件已发送")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
