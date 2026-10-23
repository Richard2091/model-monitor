# -*- coding: utf-8 -*-
"""邮件告警模块：模型状态变化（正常/异常）时，每个模型单独发送一封告警邮件。

规则：仅在状态变化时发送；每个模型的状态变化单独发一封；发送失败不更新状态，
下一轮自动重试；每日发送次数有上限。命令行 `python alerts.py --test` 可发送测试邮件。
"""

import logging
import sys
from datetime import datetime

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


def _model_title(model, names):
    """生成模型的展示名：显示名与模型ID不同时写成“显示名（模型ID）”。

    @param model 模型ID
    @param names 模型显示名映射
    @return 展示用字符串
    """
    # 无显示名或与模型ID相同时只写模型ID
    shown = names.get(model)
    return f"{shown}（{model}）" if shown and shown != model else model


def _down_minutes(changed_at, now):
    """根据上次状态变化时间计算故障持续的分钟数。

    @param changed_at 上次变化时间（ISO 字符串），可为 None
    @param now 当前本地时间（datetime）
    @return 分钟数（整数）；无法计算时返回 None
    """
    # 解析失败或时间倒挂都视为算不出
    if not changed_at:
        return None
    try:
        delta = now - datetime.fromisoformat(changed_at)
    except (ValueError, TypeError):
        return None
    if delta.total_seconds() < 0:
        return None
    return int(delta.total_seconds() // 60)


def build_message(change, names, now, down_minutes=None):
    """生成单个模型告警邮件的主题与纯文本正文。

    @param change 状态变化，含 model、recovered、status_code、latency_ms、message
    @param names 模型显示名映射
    @param now 当前本地时间（datetime）
    @param down_minutes 恢复时的故障持续分钟数，None 则不写该行
    @return (subject, body)
    """
    title = _model_title(change["model"], names)
    # 主题：恢复 / 带状态码的异常 / 无状态码（网络错误或超时）的异常
    if change["recovered"]:
        subject = f"【模型监控】{title} 已恢复"
    elif change.get("status_code"):
        subject = f"【模型监控】{title} 异常：HTTP {change['status_code']}"
    else:
        subject = f"【模型监控】{title} 异常：请求失败"
    latency = change.get("latency_ms")
    message = str(change.get("message") or "")[:_MESSAGE_MAX_LEN]
    lines = [
        f"时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
        f"模型：{title}",
        f"状态：{'恢复' if change['recovered'] else '异常'}",
    ]
    if change["recovered"] and down_minutes is not None:
        lines.append(f"故障持续：{down_minutes} 分钟")
    lines.append(f"HTTP 状态码：{change.get('status_code')}")
    lines.append(f"延迟：{str(latency) + ' ms' if latency is not None else '-'}")
    lines.append(f"错误信息：{message or '-'}")
    # 配置了监控页链接时附在末尾
    if config.ALERT_PAGE_URL:
        lines.append("")
        lines.append(f"监控页：{config.ALERT_PAGE_URL}")
    return subject, "\n".join(lines) + "\n"


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
    # 告警关闭：只更新状态，避免以后开启时把旧故障当新故障
    if not is_enabled():
        database.save_alert_states([(c["model"], bool(c["ok"]), now_iso) for c in changes])
        return
    names = _display_names(targets)
    times = database.get_alert_changed_at()
    sent_today = database.count_alert_emails(now.strftime("%Y-%m-%d"))
    limit_warned = False
    # 逐个模型单独发信，互不影响
    for change in changes:
        state = [(change["model"], bool(change["ok"]), now_iso)]
        # 达到每日上限：不发信，但照样更新状态，免得第二天补发旧变化
        if sent_today >= config.ALERT_DAILY_LIMIT:
            if not limit_warned:
                logger.warning("今日告警邮件已达上限 %s，剩余状态变化只更新状态不发信", config.ALERT_DAILY_LIMIT)
                limit_warned = True
            database.save_alert_states(state)
            continue
        down = _down_minutes(times.get(change["model"]), now) if change["recovered"] else None
        subject, body = build_message(change, names, now, down)
        sent_today += 1
        try:
            send_mail(subject, body)
        except Exception as exc:
            # 发送失败：记录失败，只有该模型不写状态，下一轮仍为变化会自动重试
            logger.error("告警邮件发送失败（%s）: %s", change["model"], exc)
            database.record_alert_email(subject, False, str(exc)[:500])
            continue
        # 发送成功后才落该模型状态
        database.record_alert_email(subject, True)
        database.save_alert_states(state)


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
