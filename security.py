# -*- coding: utf-8 -*-
"""敏感配置安全工具：API Key 加密与管理员凭据校验。"""

import base64
import hashlib
import hmac
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SecretError(ValueError):
    """敏感配置无法安全处理时抛出的异常。"""


def _decode_master_key(raw_key):
    """解析 Base64URL 主密钥并校验 AES-256 所需长度。"""
    # 校验主密钥是否提供
    if not raw_key:
        raise SecretError("MODEL_MONITOR_MASTER_KEY 未配置")
    try:
        # 补齐 Base64URL 填充并解码
        key = base64.urlsafe_b64decode(raw_key + "=" * (-len(raw_key) % 4))
    except (ValueError, TypeError) as exc:
        raise SecretError("MODEL_MONITOR_MASTER_KEY 格式无效") from exc
    # 校验密钥必须为 32 字节
    if len(key) != 32:
        raise SecretError("MODEL_MONITOR_MASTER_KEY 必须解码为 32 字节")
    return key


def encrypt_secret(secret, master_key, associated_data):
    """使用 AES-256-GCM 加密敏感字符串。

    @param secret 待加密字符串
    @param master_key Base64URL 编码的主密钥
    @param associated_data 绑定厂商和版本的认证数据
    @return (密文, nonce)
    """
    # 解析并校验主密钥
    key = _decode_master_key(master_key)
    # 生成 96 位随机 nonce 并执行认证加密
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, str(secret).encode("utf-8"), associated_data.encode("utf-8"))
    return ciphertext, nonce


def decrypt_secret(ciphertext, nonce, master_key, associated_data):
    """使用 AES-256-GCM 解密敏感字符串。"""
    # 解析并校验主密钥
    key = _decode_master_key(master_key)
    try:
        # 校验认证数据并解密密文
        plain = AESGCM(key).decrypt(bytes(nonce), bytes(ciphertext), associated_data.encode("utf-8"))
    except Exception as exc:
        raise SecretError("敏感配置解密失败") from exc
    # 返回 UTF-8 明文，仅供服务内部使用
    return plain.decode("utf-8")


def hash_token(token):
    """计算会话令牌的 SHA-256 摘要，数据库不保存令牌明文。"""
    # 对令牌做稳定摘要
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_admin_password(expected, supplied):
    """以常量时间比较管理员密码，避免响应暴露凭据差异。"""
    # 将输入规范化为字符串后执行常量时间比较
    return hmac.compare_digest(str(expected or "").encode("utf-8"), str(supplied or "").encode("utf-8"))
