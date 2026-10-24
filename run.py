#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 每日积分任务（GitHub Actions 云端版）。

自包含、仅用标准库，不依赖本机登录态、不依赖 WorkBuddy 客户端在线。流程：
  1. 用长效 refresh_token 换当日 accessToken
  2. 查签到状态 -> 未签到则签到（幂等）
  3. 派猫猫旅行闭环：已到达 -> 领取；空闲且未达上限 -> 派出
  4. 汇总播报并通过 SMTP 发送邮件

环境变量（全部由 GitHub Secrets 注入，脚本本身不含任何凭据）：
  WB_RT          必填，长效 refresh_token
  WB_MAIL_USER   必填，发件邮箱
  WB_MAIL_PASS   必填，邮箱 SMTP 授权码（不是登录密码）
  WB_MAIL_TO     选填，收件人，缺省等于 WB_MAIL_USER
  WB_MAIL_HOST   选填，SMTP 主机，缺省 smtp.qq.com
  WB_MAIL_PORT   选填，SMTP 端口，缺省 465（SSL）
  WB_LOCATION    选填，派遣地点 id（1-4），缺省随机
  DRY_RUN        选填，设为 1 则只读演练（不签到、不派遣、不领取、不发邮件）

退出码：0 成功（含"今日已签到"）/ 1 失败 / 2 缺少必需环境变量。
"""

import json
import os
import random
import smtplib
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

# ---- 接口常量（与 WorkBuddy 开放接口一致，改动前先看 references/api.md） ----
REFRESH_URL = "https://copilot.tencent.com/v2/plugin/auth/token/refresh"
API = "https://www.codebuddy.cn"
P_CHECKIN_STATUS = "/v2/billing/meter/checkin-activity-status"
P_CHECKIN = "/v2/billing/meter/daily-checkin"
P_TRAVEL_STATUS = "/activity/growth/buddy/travel/status"
P_TRAVEL_CLAIM = "/activity/growth/buddy/travel/claim"
P_TRAVEL_DEPART = "/activity/growth/buddy/travel/depart"
P_TRAVEL_CONFIG = "/activity/growth/buddy/travel/config"
P_ENERGY = "/activity/growth/energy"

TIMEOUT = 20
TZ = timezone(timedelta(hours=8))
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"
UA = "workbuddy-gha-checkin/1.0"

energy_balance_cache = None


def log(msg):
    print(msg, flush=True)


def http(url, token=None, method="GET", body=None, headers=None, retries=3):
    """带重试的 JSON 请求。返回 (http_status, dict)。网络异常返回 (0, {...})。"""
    last = (0, {"code": -1, "msg": "no attempt"})
    for attempt in range(retries):
        data = None
        if method == "POST":
            data = json.dumps(body if body is not None else {}).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", UA)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", "replace")
                try:
                    return resp.status, json.loads(raw)
                except json.JSONDecodeError:
                    return resp.status, {"code": -1, "msg": "非 JSON 响应: " + raw[:200]}
        except urllib.error.HTTPError as exc:
            raw = ""
            try:
                raw = exc.read().decode("utf-8", "replace")
                return exc.code, json.loads(raw)
            except Exception:
                return exc.code, {"code": -1, "msg": "HTTP %s %s" % (exc.code, raw[:200])}
        except Exception as exc:  # noqa: BLE001
            last = (0, {"code": -1, "msg": str(exc)[:200]})
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
    return last


def refresh_access_token(rt):
    """用长效令牌换当日 accessToken。令牌只存在于内存，绝不打印。"""
    status, body = http(
        REFRESH_URL, method="POST", body={},
        headers={
            "X-Refresh-Token": rt,
            "X-Auth-Refresh-Source": "plugin",
            "X-Domain": "copilot.tencent.com",
        },
    )
    data = body.get("data") or {}
    token = data.get("accessToken") or ""
    if body.get("code") != 0 or not token:
        raise RuntimeError(
            "长效令牌刷新失败 code=%s %s（令牌可能已失效，需重新导出并更新 Secret）"
            % (body.get("code"), body.get("msg"))
        )
    return token


def get_status(token):
    status, body = http(API + P_CHECKIN_STATUS, token, method="POST", body={})
    if status != 200 or body.get("code") != 0:
        raise RuntimeError("查询签到状态失败：HTTP %s %s" % (status, body.get("msg")))
    return body.get("data") or {}


def do_checkin(token):
    """先查后签，幂等。返回 (动作, 说明)。"""
    data = get_status(token)
    if data.get("today_checked_in"):
        return "skipped", "今日已签到，无需重复打卡"
    if DRY_RUN:
        return "dry_run", "（演练）本应执行签到"
    status, body = http(API + P_CHECKIN, token, method="POST", body={})
    code = body.get("code")
    if status == 200 and code == 0:
        return "checked_in", body.get("msg") or "签到成功"
    if code == 10001 or "已签到" in (body.get("msg") or ""):
        return "skipped", body.get("msg") or "今日已签到"
    return "failed", "签到失败：HTTP %s code=%s %s" % (status, code, body.get("msg"))


def travel_status(token):
    status, body = http(API + P_TRAVEL_STATUS, token)
    if status != 200 or body.get("code") != 0:
        return None
    return body.get("data") or {}


def travel_config(token):
    status, body = http(API + P_TRAVEL_CONFIG, token)
    if status != 200 or body.get("code") != 0:
        return []
    return (body.get("data") or {}).get("locations") or []


def fmt_remaining(seconds):
    if seconds is None or seconds <= 0:
        return "已到达"
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return "%d 小时 %d 分" % (hours, minutes)
    return "%d 分钟" % minutes


def run_travel(token):
    """先领后派闭环；达每日上限绝不发写请求。"""
    logs = []
    st = travel_status(token)
    if st is None:
        return None, ["查询旅行状态失败（只读接口异常，已跳过）"]

    # 1) 已到达 -> 领取
    if st.get("state") == "arrived":
        if DRY_RUN:
            logs.append("（演练）本应领取旅行奖励 %s 分" % st.get("reward_credit"))
        else:
            status, body = http(API + P_TRAVEL_CLAIM, token, method="POST", body={})
            if status == 200 and body.get("code") == 0:
                got = (body.get("data") or {}).get("reward_credit") or st.get("reward_credit")
                logs.append("已领取旅行奖励 +%s 积分" % got)
            else:
                logs.append("领取失败：code=%s %s" % (body.get("code"), body.get("msg")))
        st = travel_status(token) or st

    # 2) 空闲且未达上限 -> 派出
    if st.get("state") == "idle":
        if st.get("daily_limit_reached"):
            logs.append("今日派遣已达上限，跳过派出")
        else:
            locations = travel_config(token)
            choices = {str(loc.get("id")): loc.get("name") for loc in locations}
            want = os.environ.get("WB_LOCATION", "").strip()
            if want in choices:
                loc_id, loc_name = int(want), choices[want]
            elif choices:
                loc_id = int(random.choice(list(choices.keys())))
                loc_name = choices[str(loc_id)]
            else:
                loc_id, loc_name = random.randint(1, 4), "随机地点"
            if DRY_RUN:
                logs.append("（演练）本应派往【%s】" % loc_name)
            else:
                status, body = http(API + P_TRAVEL_DEPART, token, method="POST",
                                    body={"location_id": loc_id})
                if status == 200 and body.get("code") == 0:
                    logs.append("已派出 Buddy 前往【%s】" % loc_name)
                else:
                    logs.append("派出失败：code=%s %s" % (body.get("code"), body.get("msg")))
            st = travel_status(token) or st
    elif st.get("state") == "traveling":
        logs.append("旅行中，不重复派出")
    if not logs:
        logs.append("无需操作")
    return st, logs


def energy_balance(token):
    status, body = http(API + P_ENERGY, token)
    if status == 200 and body.get("code") == 0:
        return (body.get("data") or {}).get("balance")
    return None


def build_report(status, travel, travel_logs, checkin):
    action, msg = checkin
    today = datetime.now(TZ)
    lines = []
    lines.append("WorkBuddy 积分播报 · %s" % today.strftime("%Y-%m-%d %H:%M"))
    lines.append("")
    lines.append("【签到】%s" % {"checked_in": "签到成功", "skipped": "今日已签到",
                              "dry_run": "演练模式", "failed": "签到失败"}.get(action, action))
    lines.append("  说明：%s" % msg)
    lines.append("  积分余额：%s 分" % status.get("total_credits"))
    lines.append("  连续登录：%s 天（今日已记录：%s）"
                 % (status.get("streak_days"), "是" if status.get("today_checked_in") else "否"))
    lines.append("  今日可获得：%s 分" % status.get("today_credit"))
    week = status.get("week_progress") or []
    marks = "".join("●" if x else "○" for x in week)
    lines.append("  本周进度：%s（%s/7）" % (marks, status.get("week_checkin_days")))
    lines.append("  本期活动：%s 第 %s 期（%s ~ %s）"
                 % (status.get("activity_name"), status.get("season"),
                    (status.get("start_time") or "")[:10], (status.get("end_time") or "")[:10]))
    lines.append("")

    lines.append("【派猫猫旅行】")
    if travel:
        state = travel.get("state")
        state_text = {"idle": "空闲", "traveling": "旅行中", "arrived": "已到达待领取"}.get(state, state)
        loc = (travel.get("location") or {}).get("name") or "-"
        lines.append("  状态：%s · 地点：%s" % (state_text, loc))
        if state == "traveling":
            now = travel.get("server_now") or int(time.time())
            lines.append("  到达倒计时：%s" % fmt_remaining((travel.get("arrive_at") or 0) - now))
        lines.append("  可领积分：%s 分" % travel.get("reward_credit"))
        lines.append("  今日派遣：%s" % ("已达上限" if travel.get("daily_limit_reached") else "还可派出"))
    else:
        lines.append("  状态：查询失败（不影响签到）")
    for item in travel_logs:
        lines.append("  · %s" % item)
    lines.append("")

    if energy_balance_cache is not None:
        lines.append("【能量】余额 %s" % energy_balance_cache)
        lines.append("")
    lines.append("— 由 GitHub Actions 自动执行（WorkBuddy 积分云端签到）")
    return "\n".join(lines)


def send_mail(subject, content):
    user = os.environ["WB_MAIL_USER"].strip()
    password = os.environ["WB_MAIL_PASS"].strip()
    to = (os.environ.get("WB_MAIL_TO") or "").strip() or user
    host = os.environ.get("WB_MAIL_HOST", "smtp.qq.com").strip()
    port = int(os.environ.get("WB_MAIL_PORT", "465"))
    msg = MIMEText(content, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("WorkBuddy 积分助手", "utf-8")), user))
    msg["To"] = to
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=20)
    else:
        server = smtplib.SMTP(host, port, timeout=20)
        server.starttls()
    try:
        server.login(user, password)
        server.sendmail(user, [x.strip() for x in to.split(",") if x.strip()], msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass
    return to


def main():
    global energy_balance_cache
    rt = (os.environ.get("WB_RT") or "").strip()
    if not rt:
        log("错误：缺少环境变量 WB_RT（长效令牌）")
        return 2

    log("模式：%s" % ("DRY_RUN 演练" if DRY_RUN else "实际执行"))
    try:
        token = refresh_access_token(rt)
        log("令牌刷新成功")
        checkin = do_checkin(token)
        log("签到：%s - %s" % checkin)
        travel, travel_logs = run_travel(token)
        for item in travel_logs:
            log("旅行：%s" % item)
        energy_balance_cache = energy_balance(token)
        status = get_status(token)
    except Exception as exc:  # noqa: BLE001
        log("执行失败：%s" % exc)
        return 1

    report = build_report(status, travel, travel_logs, checkin)
    print("\n" + report + "\n")

    if DRY_RUN:
        log("演练模式，未发送邮件")
        return 0
    if checkin[0] == "failed":
        log("签到失败，不发送'成功'邮件（由 GitHub 失败通知提醒）")
        return 1
    try:
        to = send_mail("WorkBuddy 积分播报 · 余额 %s 分 · 连续 %s 天"
                       % (status.get("total_credits"), status.get("streak_days")), report)
        log("邮件已发送至 %s" % to)
    except Exception as exc:  # noqa: BLE001
        log("邮件发送失败：%s" % exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
