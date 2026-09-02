# -*- coding: utf-8 -*-
"""SQLite 数据层：配置迁移、运行设置与探测历史。"""

import json
import logging
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

import config
from security import encrypt_secret, decrypt_secret, SecretError

logger = logging.getLogger(__name__)
SCHEMA_VERSION = 1


def _connect():
    """创建启用外键与超时的 SQLite 连接。"""
    # 建立数据库连接并启用外键约束
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """初始化历史表和配置表，并在首次启动时导入旧环境配置。

    @return None
    """
    # 建立目录与数据库连接
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = _connect()
    try:
        # 启用 WAL 并开启迁移事务
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN")
        # 保留既有探测历史表
        conn.execute("""CREATE TABLE IF NOT EXISTS probe_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT NOT NULL, probed_at TEXT NOT NULL,
            ok INTEGER NOT NULL, latency_ms INTEGER, status_code INTEGER, message TEXT)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_probe_model_time ON probe_records(model, probed_at)")
        # 创建管理配置、会话和审计表
        conn.execute("""CREATE TABLE IF NOT EXISTS monitor_vendors (
            id INTEGER PRIMARY KEY AUTOINCREMENT, vendor_key TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL, base_url TEXT NOT NULL,
            api_key_ciphertext BLOB, api_key_nonce BLOB, api_key_key_version INTEGER NOT NULL DEFAULT 1,
            enabled INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS monitor_models (
            id INTEGER PRIMARY KEY AUTOINCREMENT, vendor_id INTEGER NOT NULL,
            model_key TEXT NOT NULL UNIQUE, display_name TEXT, enabled INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            deleted_at TEXT, FOREIGN KEY(vendor_id) REFERENCES monitor_vendors(id))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS monitor_settings (
            id INTEGER PRIMARY KEY CHECK(id=1), monitor_windows TEXT NOT NULL,
            probe_interval_min INTEGER NOT NULL DEFAULT 5, slow_threshold_ms INTEGER NOT NULL DEFAULT 10000,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        # 为已创建的旧设置表补充配置版本字段，保证升级幂等
        settings_columns = {row[1] for row in conn.execute("PRAGMA table_info(monitor_settings)").fetchall()}
        if "version" not in settings_columns:
            conn.execute("ALTER TABLE monitor_settings ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        conn.execute("""CREATE TABLE IF NOT EXISTS admin_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT NOT NULL UNIQUE,
            csrf_token_hash TEXT NOT NULL, username TEXT NOT NULL, created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL, expires_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS admin_audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, action TEXT NOT NULL,
            resource_type TEXT NOT NULL, resource_id TEXT, detail_json TEXT, created_at TEXT NOT NULL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_monitor_models_vendor ON monitor_models(vendor_id, enabled, sort_order)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON admin_sessions(expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON admin_audit_logs(created_at)")
        # 首次升级仅在设置为空时导入旧 .env
        if conn.execute("SELECT COUNT(*) FROM monitor_settings").fetchone()[0] == 0:
            now = config.now_local().isoformat()
            conn.execute("INSERT INTO monitor_settings(id,monitor_windows,probe_interval_min,slow_threshold_ms,version,created_at,updated_at) VALUES(1,?,?,?,?,?,?)", (json.dumps([{"start": f"{config.TIME_START//60:02d}:{config.TIME_START%60:02d}", "end": f"{config.TIME_END//60:02d}:{config.TIME_END%60:02d}"}], ensure_ascii=False), config.PROBE_INTERVAL_MIN, config.SLOW_THRESHOLD_MS, 1, now, now))
        if conn.execute("SELECT COUNT(*) FROM monitor_vendors WHERE deleted_at IS NULL").fetchone()[0] == 0:
            now = config.now_local().isoformat()
            vendor_key = "default-gateway"
            conn.execute("INSERT INTO monitor_vendors(vendor_key,display_name,base_url,enabled,sort_order,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (vendor_key, "默认网关", config.SUB2API_URL.rstrip("/"), 1, 0, now, now))
            vendor_id = conn.execute("SELECT id FROM monitor_vendors WHERE vendor_key=?", (vendor_key,)).fetchone()[0]
            cipher = nonce = None
            if config.API_KEY:
                if not config.MODEL_MONITOR_MASTER_KEY:
                    raise RuntimeError("首次迁移 API Key 前必须配置 MODEL_MONITOR_MASTER_KEY")
                cipher, nonce = encrypt_secret(config.API_KEY, config.MODEL_MONITOR_MASTER_KEY, f"model-monitor/vendor/{vendor_key}/api-key/v1")
                conn.execute("UPDATE monitor_vendors SET api_key_ciphertext=?,api_key_nonce=? WHERE id=?", (cipher, nonce, vendor_id))
            for idx, model in enumerate(config.MODELS):
                conn.execute("INSERT INTO monitor_models(vendor_id,model_key,enabled,sort_order,created_at,updated_at) VALUES(?,?,?,?,?,?)", (vendor_id, model, 1, idx, now, now))
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        # 迁移失败时回滚，避免留下半套配置
        conn.rollback()
        raise
    finally:
        conn.close()


def save_record(model, result):
    """写入一次探测结果。"""
    # 保存单次探测记录
    conn = _connect()
    try:
        conn.execute("INSERT INTO probe_records(model,probed_at,ok,latency_ms,status_code,message) VALUES(?,?,?,?,?,?)", (model, config.now_local().isoformat(), 1 if result["ok"] else 0, result.get("latency_ms"), result.get("status_code"), result.get("message", "")))
        conn.commit()
    finally:
        conn.close()


def build_query(date_str, targets=None, interval_min=None, windows=None, slow_ms=None):
    """按当前配置快照查询某日探测槽位。"""
    # 使用传入快照，避免查询过程混用新旧配置
    if targets is None:
        from config_manager import get_snapshot
        snapshot = get_snapshot()
        targets, interval_min, windows, slow_ms = snapshot.models, snapshot.probe_interval_min, snapshot.windows, snapshot.slow_threshold_ms
    conn = _connect()
    try:
        cur = conn.cursor(); out=[]
        time_start=min(w[0] for w in windows); time_end=max(w[1] for w in windows); total=(time_end-time_start)//interval_min+1
        for target in targets:
            rows=cur.execute("SELECT probed_at,ok,latency_ms,status_code,message FROM probe_records WHERE model=? AND substr(probed_at,1,10)=? ORDER BY probed_at", (target.model_key,date_str)).fetchall()
            by_slot={}
            for probed_at,ok,latency,status,msg in rows:
                try: dt=datetime.fromisoformat(probed_at)
                except ValueError: continue
                idx=round((dt.hour*60+dt.minute-time_start)/interval_min)
                if 0<=idx<total and any(s<=time_start+idx*interval_min<=e for s,e in windows): by_slot[idx]={"ok":bool(ok),"latency_ms":latency,"status_code":status,"message":msg}
            slots=[]
            for i in range(total):
                minute=time_start+i*interval_min; item={"time":f"{minute//60:02d}:{minute%60:02d}"}
                item.update(by_slot.get(i,{"ok":None,"latency_ms":None,"status_code":None,"message":""})); slots.append(item)
            out.append({"model":target.model_key,"display_name":target.display_name or target.model_key,"vendor_key":target.vendor_key,"vendor_display_name":target.vendor_display_name,"slots":slots})
        return {"date":date_str,"interval":interval_min,"slow_ms":slow_ms,"windows":", ".join(f"{s//60:02d}:{s%60:02d}-{e//60:02d}:{e%60:02d}" for s,e in windows),"models":out}
    finally: conn.close()


def _now(): return config.now_local().isoformat()


def load_runtime_rows():
    """读取启用的厂商、模型和全局设置，供配置快照使用。"""
    # 查询当前有效配置
    conn=_connect()
    try:
        vendors=conn.execute("SELECT id,vendor_key,display_name,base_url,api_key_ciphertext,api_key_nonce,api_key_key_version,enabled FROM monitor_vendors WHERE deleted_at IS NULL ORDER BY sort_order,id").fetchall()
        settings=conn.execute("SELECT monitor_windows,probe_interval_min,slow_threshold_ms,version FROM monitor_settings WHERE id=1").fetchone()
        models=conn.execute("SELECT m.model_key,m.display_name,m.enabled,v.vendor_key,v.display_name,v.base_url,v.api_key_ciphertext,v.api_key_nonce,v.api_key_key_version,v.enabled FROM monitor_models m JOIN monitor_vendors v ON v.id=m.vendor_id WHERE m.deleted_at IS NULL ORDER BY v.sort_order,v.id,m.sort_order,m.id").fetchall()
        return vendors, models, settings
    finally: conn.close()


def list_admin_config():
    """读取管理页配置，不返回 API Key 明文。"""
    # 查询厂商与模型，并脱敏密钥状态
    conn=_connect()
    try:
        vendors=conn.execute("SELECT id,vendor_key,display_name,base_url,api_key_ciphertext,enabled,sort_order FROM monitor_vendors WHERE deleted_at IS NULL ORDER BY sort_order,id").fetchall()
        result=[]
        for vid,key,name,url,cipher,enabled,order in vendors:
            models=conn.execute("SELECT model_key,display_name,enabled,sort_order FROM monitor_models WHERE vendor_id=? AND deleted_at IS NULL ORDER BY sort_order,id",(vid,)).fetchall()
            result.append({"vendor_key":key,"display_name":name,"base_url":url,"api_key_configured":bool(cipher),"enabled":bool(enabled),"sort_order":order,"models":[{"model_key":m,"display_name":d or "","enabled":bool(e),"sort_order":o} for m,d,e,o in models]})
        settings=conn.execute("SELECT monitor_windows,probe_interval_min,slow_threshold_ms,version,updated_at FROM monitor_settings WHERE id=1").fetchone()
        windows=json.loads(settings[0]) if settings else []
        generation=settings[3] if settings else 1
        return {"generation":generation,"settings":{"monitor_windows":windows,"probe_interval_min":settings[1] if settings else 5,"slow_threshold_ms":settings[2] if settings else 10000,"updated_at":settings[4] if settings else ""},"vendors":result}
    finally: conn.close()


def _audit(username, action, resource_type, resource_id, detail=None, conn=None):
    """记录不含敏感明文的管理审计日志。"""
    own=conn is None; conn=conn or _connect()
    try:
        conn.execute("INSERT INTO admin_audit_logs(username,action,resource_type,resource_id,detail_json,created_at) VALUES(?,?,?,?,?,?)",(username,action,resource_type,resource_id,json.dumps(detail or {},ensure_ascii=False),_now()))
        if own: conn.commit()
    finally:
        if own: conn.close()


def update_settings(payload, username):
    """校验并保存监控设置，然后由配置管理器刷新快照。"""
    # 校验时间段和数值参数
    windows=payload.get("monitor_windows")
    if not isinstance(windows,list) or not windows: raise ValueError("至少需要一个监控时间段")
    parsed=[]
    for item in windows:
        start=config._parse_time(str(item.get("start",""))); end=config._parse_time(str(item.get("end","")))
        if start>=end: raise ValueError("监控时间段的开始时间必须早于结束时间")
        parsed.append({"start":f"{start//60:02d}:{start%60:02d}","end":f"{end//60:02d}:{end%60:02d}"})
    interval=int(payload.get("probe_interval_min",5)); slow=int(payload.get("slow_threshold_ms",10000))
    if interval<=0 or slow<0: raise ValueError("探测间隔必须大于 0，慢响应阈值不能小于 0")
    conn=_connect()
    try:
        conn.execute("UPDATE monitor_settings SET monitor_windows=?,probe_interval_min=?,slow_threshold_ms=?,version=version+1,updated_at=? WHERE id=1",(json.dumps(parsed,ensure_ascii=False),interval,slow,_now()))
        _audit(username,"update","settings","1",{"probe_interval_min":interval,"slow_threshold_ms":slow},conn); conn.commit()
    finally: conn.close()


def _normalize_url(url):
    """校验并规范化厂商 Base URL。

    @param url 待校验的厂商基础地址。
    @return 去除末尾斜杠后的合法 HTTP(S) 地址。
    """
    # 使用标准库校验 URL 协议和控制字符
    from urllib.parse import urlparse
    value=str(url or "").strip().rstrip("/"); parsed=urlparse(value)
    if parsed.scheme not in ("http","https") or not parsed.netloc or any(ord(c)<32 for c in value): raise ValueError("基础地址必须是合法的 http 或 https 地址")
    return value


def _key_fields(vendor_key, api_key):
    """加密厂商 API Key 并生成数据库字段。

    @param vendor_key 厂商标识。
    @param api_key 待加密的 API Key。
    @return 密文与随机 nonce。
    """
    # 校验主密钥并生成与厂商绑定的加密字段
    if not config.MODEL_MONITOR_MASTER_KEY:
        raise SecretError("MODEL_MONITOR_MASTER_KEY 未配置，无法保存 API Key")
    return encrypt_secret(api_key, config.MODEL_MONITOR_MASTER_KEY, f"model-monitor/vendor/{vendor_key}/api-key/v1")


def save_vendor(payload, username, vendor_key=None):
    """新增或修改厂商配置，API Key 仅在输入非空时替换。"""
    # 校验厂商字段
    body_key=str(payload.get("vendor_key") or "").strip().lower()
    if vendor_key is not None and body_key and body_key != vendor_key.lower():
        raise ValueError("路径中的厂商标识不可修改")
    key=str(vendor_key or body_key).strip().lower(); name=str(payload.get("display_name") or "").strip(); url=_normalize_url(payload.get("base_url"))
    import re
    if not re.fullmatch(r"[a-z0-9_-]+",key): raise ValueError("厂商标识只能包含小写字母、数字、中划线和下划线")
    if not name: raise ValueError("厂商名称不能为空")
    conn=_connect(); now=_now()
    try:
        old=conn.execute("SELECT id,api_key_ciphertext,api_key_nonce FROM monitor_vendors WHERE vendor_key=? AND deleted_at IS NULL",(key,)).fetchone()
        cipher=nonce=None
        secret=str(payload.get("api_key") or "")
        clear_api_key=bool(payload.get("clear_api_key", False))
        if secret:
            cipher,nonce=_key_fields(key,secret)
        elif clear_api_key:
            cipher, nonce = None, None
        if old:
            if not secret and not clear_api_key:
                cipher=old[1]; nonce=old[2]
            conn.execute("UPDATE monitor_vendors SET display_name=?,base_url=?,api_key_ciphertext=?,api_key_nonce=?,enabled=?,sort_order=?,updated_at=? WHERE vendor_key=?",(name,url,cipher,nonce,1 if payload.get("enabled",True) else 0,int(payload.get("sort_order",0)),now,key))
            action="update"
        else:
            conn.execute("INSERT INTO monitor_vendors(vendor_key,display_name,base_url,api_key_ciphertext,api_key_nonce,enabled,sort_order,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",(key,name,url,cipher,nonce,1 if payload.get("enabled",True) else 0,int(payload.get("sort_order",0)),now,now)); action="create"
        # 更新持久化配置版本，供管理页和运行快照追踪
        conn.execute("UPDATE monitor_settings SET version=version+1,updated_at=? WHERE id=1", (now,))
        # 配置成功写入后递增业务版本，供管理页和运行快照追踪
        conn.execute("UPDATE monitor_settings SET version=version+1,updated_at=? WHERE id=1", (_now(),))
        _audit(username,action,"vendor",key,{"api_key_changed":bool(secret) or clear_api_key,"api_key_cleared":clear_api_key},conn); conn.commit()
    finally: conn.close()


def save_model(payload, username, model_key=None):
    """新增或修改模型配置。"""
    # 校验模型 ID 与厂商归属
    body_key=str(payload.get("model_key") or "").strip()
    if model_key is not None and body_key and body_key != model_key:
        raise ValueError("路径中的模型 ID 不可修改")
    key=str(model_key or body_key).strip(); vendor=str(payload.get("vendor_key") or "").strip().lower(); name=str(payload.get("display_name") or "").strip()
    if not key or any(ord(c)<32 for c in key) or "," in key: raise ValueError("模型 ID 不能为空且不能包含逗号或控制字符")
    conn=_connect(); now=_now()
    try:
        # 修改模型时允许前端省略厂商字段，沿用原有归属
        old=conn.execute("SELECT id,vendor_id FROM monitor_models WHERE model_key=? AND deleted_at IS NULL",(key,)).fetchone()
        if not vendor and old:
            vendor_row=(old[1],)
        else:
            vendor_row=conn.execute("SELECT id FROM monitor_vendors WHERE vendor_key=? AND deleted_at IS NULL",(vendor,)).fetchone()
        if not vendor_row: raise ValueError("所属厂商不存在")
        if old:
            conn.execute("UPDATE monitor_models SET vendor_id=?,display_name=?,enabled=?,sort_order=?,updated_at=? WHERE model_key=?",(vendor_row[0],name or None,1 if payload.get("enabled",True) else 0,int(payload.get("sort_order",0)),now,key)); action="update"
        else:
            conn.execute("INSERT INTO monitor_models(vendor_id,model_key,display_name,enabled,sort_order,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",(vendor_row[0],key,name or None,1 if payload.get("enabled",True) else 0,int(payload.get("sort_order",0)),now,now)); action="create"
        # 更新持久化配置版本，供管理页和运行快照追踪
        conn.execute("UPDATE monitor_settings SET version=version+1,updated_at=? WHERE id=1", (now,))
        # 配置成功写入后递增业务版本，供管理页和运行快照追踪
        conn.execute("UPDATE monitor_settings SET version=version+1,updated_at=? WHERE id=1", (_now(),))
        _audit(username,action,"model",key,{},conn); conn.commit()
    finally: conn.close()


def delete_model(model_key, username):
    """删除模型配置及其探测历史，删除前创建数据库备份。

    @param model_key 要删除的模型 ID。
    @param username 执行操作的管理员账号。
    """
    # 锁定探测轮次，避免旧任务在删除后重新写回历史
    from monitor import probe_lock
    with probe_lock():
        # 删除前创建 SQLite 在线备份
        backup_database("delete-model")
        conn=_connect()
        try:
            # 查询模型并删除关联历史与配置
            row=conn.execute("SELECT id FROM monitor_models WHERE model_key=? AND deleted_at IS NULL",(model_key,)).fetchone()
            if not row: raise ValueError("模型不存在")
            conn.execute("DELETE FROM probe_records WHERE model=?",(model_key,))
            conn.execute("DELETE FROM monitor_models WHERE id=?",(row[0],))
            conn.execute("UPDATE monitor_settings SET version=version+1,updated_at=? WHERE id=1", (_now(),))
            _audit(username,"delete","model",model_key,{"history_deleted":True},conn)
            conn.commit()
        finally:
            conn.close()

def delete_vendor(vendor_key, username):
    """删除厂商、所属模型及相关探测历史，删除前创建数据库备份。"""
    # 先锁定探测轮次，再备份并删除厂商及其全部模型历史
    from monitor import probe_lock
    with probe_lock():
        backup_database("delete-vendor")
        conn=_connect()
        try:
            row=conn.execute("SELECT id FROM monitor_vendors WHERE vendor_key=? AND deleted_at IS NULL",(vendor_key,)).fetchone()
            if not row: raise ValueError("厂商不存在")
            keys=[r[0] for r in conn.execute("SELECT model_key FROM monitor_models WHERE vendor_id=?",(row[0],)).fetchall()]
            for key in keys: conn.execute("DELETE FROM probe_records WHERE model=?",(key,))
            conn.execute("DELETE FROM monitor_models WHERE vendor_id=?",(row[0],)); conn.execute("DELETE FROM monitor_vendors WHERE id=?",(row[0],))
            conn.execute("UPDATE monitor_settings SET version=version+1,updated_at=? WHERE id=1", (_now(),))
            _audit(username,"delete","vendor",vendor_key,{"models_deleted":len(keys),"history_deleted":True},conn); conn.commit()
        finally: conn.close()


def get_delete_preview(resource_type, resource_key):
    """查询删除配置将影响的模型和历史记录数量。

    @param resource_type 资源类型，支持 vendor 或 model。
    @param resource_key 厂商标识或模型 ID。
    @return 删除影响范围摘要，不包含敏感信息。
    """
    conn=_connect()
    try:
        if resource_type == "model":
            row=conn.execute("SELECT model_key FROM monitor_models WHERE model_key=? AND deleted_at IS NULL",(resource_key,)).fetchone()
            if not row: raise ValueError("模型不存在")
            count=conn.execute("SELECT COUNT(*) FROM probe_records WHERE model=?",(resource_key,)).fetchone()[0]
            return {"resource_type":"model","resource_key":resource_key,"model_count":1,"history_count":count}
        if resource_type == "vendor":
            row=conn.execute("SELECT id FROM monitor_vendors WHERE vendor_key=? AND deleted_at IS NULL",(resource_key,)).fetchone()
            if not row: raise ValueError("厂商不存在")
            model_count=conn.execute("SELECT COUNT(*) FROM monitor_models WHERE vendor_id=? AND deleted_at IS NULL",(row[0],)).fetchone()[0]
            history_count=conn.execute("SELECT COUNT(*) FROM probe_records WHERE model IN (SELECT model_key FROM monitor_models WHERE vendor_id=?)",(row[0],)).fetchone()[0]
            return {"resource_type":"vendor","resource_key":resource_key,"model_count":model_count,"history_count":history_count}
        raise ValueError("资源类型无效")
    finally:
        conn.close()

def backup_database(reason):
    """创建 SQLite 数据库副本，供删除和回滚使用。"""
    # 使用 SQLite 在线备份同步主库与 WAL，避免只复制主文件造成备份不完整
    src=Path(config.DB_PATH); directory=src.parent/"backups"; directory.mkdir(parents=True,exist_ok=True); target=directory/f"{config.now_local().strftime('%Y%m%d%H%M%S%f')}-{reason}.db"
    if not src.exists(): raise ValueError("数据库文件不存在，无法创建备份")
    source_conn=_connect(); backup_conn=sqlite3.connect(target)
    try:
        source_conn.backup(backup_conn)
        backup_conn.commit()
    finally:
        backup_conn.close(); source_conn.close()
    return str(target)


def create_session(token_hash, csrf_hash, username, created, last_seen, expires):
    """保存管理会话。"""
    # 写入会话哈希和过期时间
    conn=_connect()
    try: conn.execute("INSERT INTO admin_sessions(token_hash,csrf_token_hash,username,created_at,last_seen_at,expires_at) VALUES(?,?,?,?,?,?)",(token_hash,csrf_hash,username,created,last_seen,expires)); conn.commit()
    finally: conn.close()


def get_session(token_hash):
    """读取有效会话并刷新最后访问时间。"""
    # 查询会话并校验绝对过期与空闲超时
    conn=_connect()
    try:
        row=conn.execute("SELECT token_hash,csrf_token_hash,username,created_at,last_seen_at,expires_at FROM admin_sessions WHERE token_hash=?",(token_hash,)).fetchone()
        if not row: return None
        now=config.now_local(); last=datetime.fromisoformat(row[4]); expires=datetime.fromisoformat(row[5])
        if now>=expires or (now-last).total_seconds()>config.ADMIN_IDLE_TIMEOUT_MINUTES*60:
            conn.execute("DELETE FROM admin_sessions WHERE token_hash=?",(token_hash,)); conn.commit(); return None
        conn.execute("UPDATE admin_sessions SET last_seen_at=? WHERE token_hash=?",(now.isoformat(),token_hash)); conn.commit(); return {"token_hash":row[0],"csrf_token_hash":row[1],"username":row[2],"csrf_token":""}
    finally: conn.close()


def rotate_csrf(token_hash, csrf_token_hash):
    """替换管理会话的 CSRF 哈希，支持页面刷新后恢复写权限。

    @param token_hash 当前会话令牌哈希。
    @param csrf_token_hash 新 CSRF 令牌哈希。
    @return None
    """
    # 将新 CSRF 哈希写入现有有效会话
    conn=_connect()
    try:
        conn.execute("UPDATE admin_sessions SET csrf_token_hash=?,last_seen_at=? WHERE token_hash=?", (csrf_token_hash,_now(),token_hash))
        conn.commit()
    finally:
        conn.close()


def delete_session(token_hash):
    """删除服务端管理会话。"""
    # 登出时立即删除令牌哈希
    conn=_connect()
    try: conn.execute("DELETE FROM admin_sessions WHERE token_hash=?",(token_hash,)); conn.commit()
    finally: conn.close()

def test_vendor(vendor_key, payload):
    """使用指定厂商配置执行一次不落库的连通性测试。"""
    # 查询厂商和可测试模型
    conn = _connect()
    try:
        vendor = conn.execute("SELECT vendor_key,display_name,base_url,api_key_ciphertext,api_key_nonce,api_key_key_version FROM monitor_vendors WHERE vendor_key=? AND deleted_at IS NULL", (vendor_key,)).fetchone()
        if not vendor:
            raise ValueError("厂商不存在")
        model_key = str(payload.get("model_key") or "").strip()
        if not model_key:
            row = conn.execute("SELECT model_key,display_name FROM monitor_models WHERE vendor_id=(SELECT id FROM monitor_vendors WHERE vendor_key=?) AND deleted_at IS NULL ORDER BY sort_order,id LIMIT 1", (vendor_key,)).fetchone()
            if row:
                model_key = row[0]
        if not model_key:
            raise ValueError("请先为厂商添加模型")
        base_url = _normalize_url(payload.get("base_url") or vendor[2])
        api_key = str(payload.get("api_key") or "")
        if not api_key and vendor[3]:
            if not config.MODEL_MONITOR_MASTER_KEY:
                raise SecretError("MODEL_MONITOR_MASTER_KEY 未配置")
            api_key = decrypt_secret(vendor[3], vendor[4], config.MODEL_MONITOR_MASTER_KEY, f"model-monitor/vendor/{vendor_key}/api-key/v{vendor[5]}")
        target = type("TestTarget", (), {"model_key": model_key, "base_url": base_url, "api_key": api_key})()
    finally:
        conn.close()
    # 调用探测函数但不写入历史记录
    from monitor import probe_model
    result = probe_model(target)
    return {"model": model_key, "ok": bool(result.get("ok")), "latency_ms": result.get("latency_ms"), "status_code": result.get("status_code"), "message": "测试成功" if result.get("ok") else "测试失败，请检查地址、密钥和模型 ID"}
