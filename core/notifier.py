"""通知：日志 + SMTP 邮件推送（QQ 邮箱）。"""
import smtplib
import ssl
from email.header import Header
from email.mime.text import MIMEText

from .logger import get_logger

log = get_logger()


class Notifier:
    def __init__(self, cfg):
        self.cfg = cfg
        self.n = cfg["notify"]

    def _send_mail(self, subject: str, body: str):
        if not self.n.get("smtp_password"):
            log.warning("未配置 SMTP 授权码，跳过邮件推送（本地日志已记录）")
            return
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = self.n["smtp_user"]
        msg["To"] = ",".join(self.n["to"])
        try:
            if self.n.get("smtp_ssl"):
                ctx = ssl.create_default_context()
                server = smtplib.SMTP_SSL(self.n["smtp_host"], self.n["smtp_port"], context=ctx, timeout=20)
            else:
                server = smtplib.SMTP(self.n["smtp_host"], self.n["smtp_port"], timeout=20)
            server.login(self.n["smtp_user"], self.n["smtp_password"])
            server.sendmail(self.n["smtp_user"], self.n["to"], msg.as_string())
            server.quit()
            log.info("邮件已发送: %s", subject)
        except Exception as e:
            log.error("邮件发送失败: %s", e)

    def notify(self, subject: str, body: str = ""):
        log.info("【通知】%s %s", subject, body)
        if self.cfg["notify"]["enabled"]:
            self._send_mail(subject, body or subject)

    def maybe_notify_result(self, operator_name: str, rarity: int, combo, reason: str):
        if rarity >= self.n.get("notify_min_rarity", 4):
            stars = "★" * rarity
            subject = f"公招出货 {rarity}★ {operator_name}"
            body = f"出货：{rarity}★ {operator_name}\n词条：{'+'.join(combo) if combo else '无'}\n说明：{reason}"
            self.notify(subject, body)
