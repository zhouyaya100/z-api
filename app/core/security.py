"""Z API - 安全模块 (JWT + 密码 + 验证码 + 登录防护)"""
import bcrypt
import jwt
import re
import time
import random
import string
import io
from collections import defaultdict
from PIL import Image, ImageDraw, ImageFont
from fastapi import HTTPException

from ..config import settings

# ---- JS Safe Integer ----
_JS_MAX_SAFE = 2**53 - 1

def safe_int(v):
    """Convert int to str if it exceeds JS safe integer range"""
    try:
        n = int(v)
    except (ValueError, TypeError):
        return v
    if abs(n) > _JS_MAX_SAFE:
        return str(v)
    return v


# ---- JWT ----
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = settings.JWT_EXPIRE_HOURS


def create_jwt(user_id: int, role: str) -> str:
    payload = {
        "sub": str(user_id),
        # role 不写入 JWT，鉴权时从 DB 实时查询
        "exp": int(time.time()) + JWT_EXPIRE_HOURS * 3600,
        "iat": int(time.time()),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_jwt(token: str) -> dict:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")


# ---- Password ----
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def validate_password_strength(password: str) -> str | None:
    """验证密码强度，返回 None=通过，返回字符串=错误信息"""
    if len(password) < settings.MIN_PASSWORD_LENGTH:
        return f"密码至少 {settings.MIN_PASSWORD_LENGTH} 个字符"
    if len(password) > 128:
        return "密码不能超过 128 个字符"
    if not re.search(r"[A-Za-z]", password):
        return "密码必须包含至少一个字母"
    if not re.search(r"\d", password):
        return "密码必须包含至少一个数字"
    return None


# ---- 登录暴力破解防护 ----
_login_attempts: dict[str, dict] = defaultdict(lambda: {"count": 0, "locked_until": 0})
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_LOCKOUT_SECONDS = 300  # 5 minutes


def check_login_rate(ip: str):
    """检查登录频率，超限则拒绝"""
    now = time.time()
    record = _login_attempts[ip]
    # 清除过期锁定
    if record["locked_until"] and now > record["locked_until"]:
        record["count"] = 0
        record["locked_until"] = 0
    # 检查是否锁定
    if record["locked_until"] and now <= record["locked_until"]:
        remaining = int(record["locked_until"] - now)
        raise HTTPException(429, f"登录尝试过于频繁，请 {remaining} 秒后再试")


def record_login_failure(ip: str):
    """记录登录失败"""
    now = time.time()
    record = _login_attempts[ip]
    record["count"] += 1
    if record["count"] >= _LOGIN_MAX_ATTEMPTS:
        record["locked_until"] = now + _LOGIN_LOCKOUT_SECONDS


def record_login_success(ip: str):
    """登录成功后清除计数"""
    if ip in _login_attempts:
        del _login_attempts[ip]


# ---- 验证码 ----
_captcha_store: dict[str, dict] = {}
_CAPTCHA_MAX_STORE = 10000  # 防止验证码存储溢出
CAPTCHA_TTL = 300  # 5 minutes
_last_captcha_cleanup = 0.0  # 上次清理时间
_CAPTCHA_CLEANUP_INTERVAL = 300  # 每 5 分钟清理一次过期验证码


def generate_captcha():
    """生成验证码图片，返回 (captcha_id, PNG bytes)"""
    # 定时清理过期验证码
    global _last_captcha_cleanup
    now = time.time()
    if now - _last_captcha_cleanup > _CAPTCHA_CLEANUP_INTERVAL:
        _last_captcha_cleanup = now
        expired = [k for k, v in _captcha_store.items() if v["expires"] < now]
        for k in expired:
            del _captcha_store[k]

    # 防止存储溢出：超过上限时清理最旧的 1/4
    if len(_captcha_store) >= _CAPTCHA_MAX_STORE:
        now = time.time()
        expired = [k for k, v in _captcha_store.items() if v["expires"] < now]
        for k in expired:
            del _captcha_store[k]
        if len(_captcha_store) >= _CAPTCHA_MAX_STORE:
            # 仍超限，按创建时间排序删除最旧的 1/4
            sorted_keys = sorted(_captcha_store.keys(), key=lambda k: _captcha_store[k]["expires"])
            for k in sorted_keys[:len(sorted_keys) // 4 + 1]:
                del _captcha_store[k]

    captcha_id = ''.join(random.choices(string.ascii_letters + string.digits, k=32))
    code = ''.join(random.choices('ABCDEFGHJKLMNPQRSTUVWXYZ23456789', k=4))
    _captcha_store[captcha_id] = {"code": code, "expires": time.time() + CAPTCHA_TTL}

    # 生成图片
    width, height = 120, 40
    img = Image.new('RGB', (width, height), color=(240, 240, 240))
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("arial.ttf", 28)
    except:
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        except:
            font = ImageFont.load_default()

    for i, ch in enumerate(code):
        x = 10 + i * 26 + random.randint(-3, 3)
        y = 2 + random.randint(-3, 5)
        color = (random.randint(30, 120), random.randint(30, 120), random.randint(30, 120))
        draw.text((x, y), ch, fill=color, font=font)

    for _ in range(4):
        x1, y1 = random.randint(0, width), random.randint(0, height)
        x2, y2 = random.randint(0, width), random.randint(0, height)
        draw.line((x1, y1, x2, y2), fill=(180, 180, 180), width=1)

    for _ in range(30):
        x, y = random.randint(0, width), random.randint(0, height)
        draw.point((x, y), fill=(150, 150, 150))

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return captcha_id, buf.getvalue()


def verify_captcha(captcha_id: str, code: str) -> bool:
    """验证验证码，验证后删除（一次性使用）"""
    entry = _captcha_store.pop(captcha_id, None)
    if not entry:
        return False
    if time.time() > entry["expires"]:
        return False
    return entry["code"].upper() == code.upper()
