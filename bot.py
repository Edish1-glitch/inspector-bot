import os
import logging
from datetime import datetime, timedelta
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters, ConversationHandler
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("USER_ID"))
TZ = pytz.timezone("Asia/Jerusalem")

SHIFTS = {
    "בוקר": {"start": (7, 0), "end": (15, 0)},
    "צהריים": {"start": (15, 0), "end": (23, 0)},
    "לילה": {"start": (23, 0), "end": (7, 0)},
    "כפולה בוקר": {"start": (7, 0), "end": (19, 0)},
    "כפולה לילה": {"start": (19, 0), "end": (7, 0)},
}

SHIFT_ALIASES = {
    "בוקר כפולה": "כפולה בוקר",
    "לילה כפולה": "כפולה לילה",
}

DAY_MAP = {
    "ראשון": 6, "שני": 0, "שלישי": 1,
    "רביעי": 2, "חמישי": 3, "שישי": 4, "שבת": 5
}
DAY_NUM_TO_HE = {v: k for k, v in DAY_MAP.items()}

WAITING_FOR_SHIFTS = 1
WAITING_FOR_UPDATE = 2

pending = {}

# approved_users: set of user_ids that are approved
# We store in memory — persists as long as bot is running
approved_users: set = set()
approved_users.add(ADMIN_ID)

HELP_TEXT = (
    "👷 *ברוך הבא לבוט תזכורות Inspector!*\n\n"
    "הבוט ישלח לך תזכורת 5 דקות לפני תחילת וסיום כל משמרת.\n"
    "אם לא תאשר — תמשיך לקבל התראות כל 2.5 דקות עד שתסמן אישור 😄\n\n"
    "את כל הפקודות הזמינות ניתן למצוא בכפתור התפריט הכחול ליד שורת ההקלדה.\n\n"
    "❓ במידה ואתה צריך עזרה או משהו לא ברור — לחץ על /help "
    "או הקלד אותו וההודעה הזו תקפוץ שוב עם הסבר על כל פקודה.\n\n"
    "📝 *סוגי משמרות:*\n"
    "בוקר | צהריים | לילה | כפולה בוקר | כפולה לילה\n\n"
    "📅 *ימים:* ראשון שני שלישי רביעי חמישי שישי שבת"
)

BACK_KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("🏠 חזרה לתפריט הראשי", callback_data="menu_main")
]])

MAIN_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("📅 הגדר משמרות לשבוע", callback_data="menu_set_shifts")],
    [
        InlineKeyboardButton("✏️ עדכן / הסר משמרת", callback_data="menu_update"),
        InlineKeyboardButton("📋 הצג משמרות", callback_data="menu_list"),
    ],
    [InlineKeyboardButton("🧪 טסט", callback_data="menu_test")],
])


def is_approved(user_id: int) -> bool:
    return user_id in approved_users


def normalize_shift(text):
    text = text.strip()
    if text in SHIFT_ALIASES:
        return SHIFT_ALIASES[text]
    if text in SHIFTS:
        return text
    return None


def parse_shifts_from_text(text):
    results = []
    lines = text.replace("\r", "\n").split("\n")
    for line in lines:
        line = line.replace(":", " ").strip()
        if not line:
            continue
        tokens = line.split()
        i = 0
        while i < len(tokens):
            if tokens[i] in DAY_MAP:
                day_str = tokens[i]
                if i + 2 < len(tokens):
                    candidate2 = tokens[i+1] + " " + tokens[i+2]
                    norm = normalize_shift(candidate2)
                    if norm:
                        results.append((day_str, norm, DAY_MAP[day_str]))
                        i += 3
                        continue
                if i + 1 < len(tokens):
                    norm = normalize_shift(tokens[i+1])
                    if norm:
                        results.append((day_str, norm, DAY_MAP[day_str]))
                        i += 2
                        continue
            i += 1
    return results


def _schedule_shift_reminder(context, user_id, weekday, time_tuple, shift_name, action, job_name):
    hour, minute = time_tuple
    now = datetime.now(TZ)
    reminder_minute = minute - 5
    reminder_hour = hour
    if reminder_minute < 0:
        reminder_minute += 60
        reminder_hour -= 1
        if reminder_hour < 0:
            reminder_hour += 24
    days_ahead = weekday - now.weekday()
    if days_ahead < 0:
        days_ahead += 7
    target = (now + timedelta(days=days_ahead)).replace(
        hour=reminder_hour, minute=reminder_minute, second=0, microsecond=0
    )
    if target < now:
        target += timedelta(weeks=1)
    context.job_queue.run_once(
        send_reminder,
        when=target,
        data={"action": action, "shift": shift_name, "job_name": job_name, "user_id": user_id},
        name=job_name,
        user_id=user_id,
        chat_id=user_id
    )


def _remove_day_jobs(context, user_id, day_str):
    removed = False
    prefix = f"shift_{user_id}_"
    for job in context.job_queue.jobs():
        if job.name in (f"{prefix}start_{day_str}", f"{prefix}end_{day_str}"):
            job.schedule_removal()
            removed = True
    return removed


def _add_day_shift(context, user_id, day_str, shift_str, day_num):
    shift = SHIFTS[shift_str]
    prefix = f"shift_{user_id}_"
    _schedule_shift_reminder(context, user_id, day_num, shift["start"], shift_str, "כניסה", f"{prefix}start_{day_str}")
    _schedule_shift_reminder(context, user_id, day_num, shift["end"], shift_str, "יציאה", f"{prefix}end_{day_str}")


async def _send_or_edit(update, text, parse_mode=None, reply_markup=None):
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)


# ── ACCESS CONTROL ────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    if is_approved(user_id):
        await update.message.reply_text(HELP_TEXT, parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
        return

    # Not approved — send request to admin
    first = user.first_name or ""
    last = user.last_name or ""
    username = f"@{user.username}" if user.username else "ללא שם משתמש"
    full_name = f"{first} {last}".strip()

    await update.message.reply_text(
        "⏳ בקשת הגישה שלך נשלחה לאדמין. תקבל הודעה ברגע שיאשרו אותך!"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ אשר גישה", callback_data=f"approve_{user_id}"),
        InlineKeyboardButton("❌ דחה", callback_data=f"deny_{user_id}"),
    ]])
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"🔔 *בקשת גישה חדשה!*\n\n"
            f"👤 שם: {full_name}\n"
            f"🆔 {username}\n\n"
            f"האם לאשר גישה לבוט?"
        ),
        parse_mode="Markdown",
        reply_markup=keyboard
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_approved(update.effective_user.id):
        return
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)


# ── SET SHIFTS ────────────────────────────────────────────────────────────────

async def set_shifts_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_approved(update.effective_user.id):
        return ConversationHandler.END
    msg = (
        "📅 שלח לי את המשמרות שלך לשבוע.\n\n"
        "אפשר בשורה אחת:\n`ראשון בוקר שני לילה שישי כפולה בוקר`\n\n"
        "או שורה אחרי שורה:\n`ראשון בוקר`\n`שני לילה`\n`שישי כפולה בוקר`"
    )
    await _send_or_edit(update, msg, parse_mode="Markdown")
    return WAITING_FOR_SHIFTS

async def set_shifts_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    parsed = parse_shifts_from_text(update.message.text)
    if not parsed:
        await update.message.reply_text(
            "❌ לא הצלחתי להבין את מה שרשמת.\n"
            "ייתכן והייתה טעות כתיב או רווח במקום לא נכון.\n\n"
            "נסה שוב, לדוגמה:\n`ראשון בוקר`\n`שני לילה`",
            parse_mode="Markdown",
            reply_markup=BACK_KEYBOARD
        )
        return WAITING_FOR_SHIFTS
    prefix = f"shift_{user_id}_"
    for job in context.job_queue.jobs():
        if job.name.startswith(prefix):
            job.schedule_removal()
    scheduled = []
    for day_str, shift_str, day_num in parsed:
        _add_day_shift(context, user_id, day_str, shift_str, day_num)
        scheduled.append(f"📌 {day_str}: {shift_str}")
    await update.message.reply_text(
        "✅ המשמרות הוגדרו:\n\n" + "\n".join(scheduled),
        reply_markup=BACK_KEYBOARD
    )
    return ConversationHandler.END


# ── UPDATE / REMOVE ───────────────────────────────────────────────────────────

async def update_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_approved(update.effective_user.id):
        return ConversationHandler.END
    msg = (
        "✏️ *עדכון / הסרת משמרת*\n\n"
        "להוספה או עדכון — שלח יום ומשמרת:\n"
        "`חמישי כפולה בוקר`\n"
        "אפשר כמה ימים: `חמישי בוקר שישי לילה`\n\n"
        "להסרה — שלח עם המילה הסר:\n"
        "`הסר ראשון`\n"
        "אפשר כמה ימים: `הסר ראשון שני`"
    )
    await _send_or_edit(update, msg, parse_mode="Markdown")
    return WAITING_FOR_UPDATE

async def update_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if text.startswith("הסר"):
        days_text = text.replace("הסר", "").strip()
        days_to_remove = [t for t in days_text.split() if t in DAY_MAP]
        if not days_to_remove:
            await update.message.reply_text(
                "❌ לא הצלחתי להבין את מה שרשמת.\n"
                "ייתכן והייתה טעות כתיב או רווח במקום לא נכון.\n\n"
                "לדוגמה: `הסר ראשון` או `הסר ראשון שני`",
                parse_mode="Markdown",
                reply_markup=BACK_KEYBOARD
            )
            return WAITING_FOR_UPDATE
        context.user_data["pending_remove_days"] = days_to_remove
        context.user_data["pending_user_id"] = user_id
        days_str = ", ".join(days_to_remove)
        await update.message.reply_text(
            f"האם אתה בטוח שאתה רוצה להסיר את המשמרות של: *{days_str}*?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ כן, הסר", callback_data="confirm_remove"),
                InlineKeyboardButton("❌ ביטול", callback_data="cancel_action")
            ]])
        )
        return ConversationHandler.END

    parsed = parse_shifts_from_text(text)
    if not parsed:
        await update.message.reply_text(
            "❌ לא הצלחתי להבין את מה שרשמת.\n"
            "ייתכן והייתה טעות כתיב או רווח במקום לא נכון.\n\n"
            "לעדכון: `חמישי בוקר`\nלהסרה: `הסר ראשון`",
            parse_mode="Markdown",
            reply_markup=BACK_KEYBOARD
        )
        return WAITING_FOR_UPDATE

    context.user_data["pending_update_shifts"] = parsed
    context.user_data["pending_user_id"] = user_id
    lines = "\n".join([f"*{d} {s}*" for d, s, _ in parsed])
    await update.message.reply_text(
        f"האם אתה בטוח שאתה רוצה להוסיף/לעדכן:\n{lines}?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ כן, עדכן", callback_data="confirm_update"),
            InlineKeyboardButton("❌ ביטול", callback_data="cancel_action")
        ]])
    )
    return ConversationHandler.END


# ── LIST ──────────────────────────────────────────────────────────────────────

async def list_shifts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    prefix = f"shift_{user_id}_start_"
    jobs = sorted(
        [j for j in context.job_queue.jobs() if j.name.startswith(prefix)],
        key=lambda j: j.next_t
    )
    if not jobs:
        await _send_or_edit(update, "אין משמרות מוגדרות כרגע.", reply_markup=BACK_KEYBOARD)
        return
    msg = "📅 *המשמרות המוגדרות:*\n\n"
    for job in jobs:
        t = job.next_t.astimezone(TZ)
        day_name = DAY_NUM_TO_HE.get(t.weekday(), "")
        msg += f"📌 {day_name} {t.strftime('%d/%m')} — {job.data['shift']}\n"
    await _send_or_edit(update, msg, parse_mode="Markdown", reply_markup=BACK_KEYBOARD)


# ── REMINDERS ─────────────────────────────────────────────────────────────────

async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    action, shift, job_name, user_id = job.data["action"], job.data["shift"], job.data["job_name"], job.data["user_id"]
    pending[job_name] = False
    emoji = "🟢" if action == "כניסה" else "🔴"
    await context.bot.send_message(
        chat_id=user_id,
        text=f"{emoji} תזכורת! עוד 5 דקות צריך לסמן *{action}* למשמרת {shift} ב-Inspector",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ סימנתי {action}!", callback_data=f"confirm_{job_name}")
        ]])
    )
    context.job_queue.run_once(
        nudge_reminder,
        when=timedelta(seconds=150),
        data={"action": action, "shift": shift, "job_name": job_name, "count": 1, "user_id": user_id},
        name=f"nudge_{job_name}_1", chat_id=user_id, user_id=user_id
    )

async def nudge_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    job_name, action, shift, count, user_id = job.data["job_name"], job.data["action"], job.data["shift"], job.data["count"], job.data["user_id"]
    if pending.get(job_name):
        return
    await context.bot.send_message(
        chat_id=user_id,
        text=f"⚠️ עוד לא סימנת *{action}* למשמרת {shift}! אל תשכח!",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ סימנתי {action}!", callback_data=f"confirm_{job_name}")
        ]])
    )
    context.job_queue.run_once(
        nudge_reminder,
        when=timedelta(seconds=150),
        data={"action": action, "shift": shift, "job_name": job_name, "count": count + 1, "user_id": user_id},
        name=f"nudge_{job_name}_{count+1}", chat_id=user_id, user_id=user_id
    )


# ── TEST ──────────────────────────────────────────────────────────────────────

async def test_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_approved(user_id):
        return
    job_name = f"test_job_{user_id}"
    pending[job_name] = False
    msg = (
        "🧪 *זוהי הודעת טסט!*\n\n"
        "🟢 תזכורת! עוד 5 דקות צריך לסמן *כניסה* למשמרת בוקר ב-Inspector\n\n"
        "אם לא תאשר — תמשיך לקבל התראות כל 2.5 דקות עד שתסמן אישור 😄"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ סימנתי כניסה!", callback_data=f"confirm_{job_name}")
    ]])
    await _send_or_edit(update, msg, parse_mode="Markdown", reply_markup=keyboard)
    context.job_queue.run_once(
        nudge_reminder,
        when=timedelta(seconds=10),
        data={"action": "כניסה", "shift": "בוקר (טסט)", "job_name": job_name, "count": 1, "user_id": user_id},
        name=f"nudge_{job_name}_1", chat_id=user_id, user_id=user_id
    )


# ── CALLBACKS ─────────────────────────────────────────────────────────────────

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    # ── Access approval (admin only) ──
    if data.startswith("approve_"):
        if user_id != ADMIN_ID:
            return
        new_user_id = int(data.replace("approve_", ""))
        approved_users.add(new_user_id)
        await query.edit_message_text(f"✅ המשתמש אושר בהצלחה!")
        await context.bot.send_message(
            chat_id=new_user_id,
            text="🎉 *קיבלת גישה לבוט!*\nעכשיו אתה יכול להתחיל להשתמש בו.",
            parse_mode="Markdown",
            reply_markup=MAIN_KEYBOARD
        )
        return

    if data.startswith("deny_"):
        if user_id != ADMIN_ID:
            return
        denied_id = int(data.replace("deny_", ""))
        await query.edit_message_text("❌ הבקשה נדחתה.")
        await context.bot.send_message(
            chat_id=denied_id,
            text="😔 בקשת הגישה שלך נדחתה. פנה למנהל לפרטים נוספים."
        )
        return

    # ── Check approval for all other actions ──
    if not is_approved(user_id):
        await query.answer("אין לך גישה לבוט.", show_alert=True)
        return

    # ── Menu ──
    if data == "menu_main":
        await query.edit_message_text(HELP_TEXT, parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
    elif data == "menu_set_shifts":
        await set_shifts_start(update, context)
    elif data == "menu_update":
        await update_start(update, context)
    elif data == "menu_list":
        await list_shifts(update, context)
    elif data == "menu_test":
        await test_reminder(update, context)

    # ── Confirm reminder ──
    elif data.startswith("confirm_") and data not in ("confirm_update", "confirm_remove"):
        job_name = data.replace("confirm_", "")
        pending[job_name] = True
        for job in context.job_queue.jobs():
            if job.name.startswith(f"nudge_{job_name}"):
                job.schedule_removal()
        now_str = datetime.now(TZ).strftime("%H:%M")
        await query.edit_message_text(
            query.message.text + f"\n\n✅ *מעולה! סומן בהצלחה בשעה {now_str}*",
            parse_mode="Markdown",
            reply_markup=BACK_KEYBOARD
        )

    elif data == "confirm_update":
        parsed = context.user_data.get("pending_update_shifts", [])
        uid = context.user_data.get("pending_user_id", user_id)
        for day_str, shift_str, day_num in parsed:
            _remove_day_jobs(context, uid, day_str)
            _add_day_shift(context, uid, day_str, shift_str, day_num)
        lines = "\n".join([f"📌 {d}: {s}" for d, s, _ in parsed])
        await query.edit_message_text("✅ עודכן בהצלחה!\n\n" + lines, parse_mode="Markdown", reply_markup=BACK_KEYBOARD)

    elif data == "confirm_remove":
        days = context.user_data.get("pending_remove_days", [])
        uid = context.user_data.get("pending_user_id", user_id)
        removed, not_found = [], []
        for day_str in days:
            (removed if _remove_day_jobs(context, uid, day_str) else not_found).append(day_str)
        msg = ""
        if removed: msg += "✅ הוסר: " + ", ".join(removed) + "\n"
        if not_found: msg += "⚠️ לא נמצאה משמרת: " + ", ".join(not_found)
        await query.edit_message_text(msg.strip(), reply_markup=BACK_KEYBOARD)

    elif data == "cancel_action":
        await query.edit_message_text("❌ הפעולה בוטלה.", reply_markup=BACK_KEYBOARD)


# ── MAIN ──────────────────────────────────────────────────────────────────────

async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "התחל / תפריט ראשי"),
        BotCommand("set_shifts", "הגדר משמרות לשבוע"),
        BotCommand("update", "עדכן, הוסף או הסר משמרת"),
        BotCommand("list_shifts", "הצג את המשמרות המוגדרות"),
        BotCommand("test", "שלח תזכורת לדוגמה"),
        BotCommand("help", "עזרה"),
    ])


def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    set_shifts_conv = ConversationHandler(
        entry_points=[CommandHandler("set_shifts", set_shifts_start)],
        states={WAITING_FOR_SHIFTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_shifts_receive)]},
        fallbacks=[],
    )
    update_conv = ConversationHandler(
        entry_points=[CommandHandler("update", update_start)],
        states={WAITING_FOR_UPDATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, update_receive)]},
        fallbacks=[],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("list_shifts", list_shifts))
    app.add_handler(CommandHandler("test", test_reminder))
    app.add_handler(set_shifts_conv)
    app.add_handler(update_conv)
    app.add_handler(CallbackQueryHandler(button_callback))
    app.run_polling()


if __name__ == "__main__":
    main()
