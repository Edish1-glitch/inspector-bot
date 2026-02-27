import os
import logging
from datetime import datetime, timedelta
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters, ConversationHandler
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("BOT_TOKEN")
USER_ID = int(os.environ.get("USER_ID"))
TZ = pytz.timezone("Asia/Jerusalem")

SHIFTS = {
    "בוקר": {"start": (7, 0), "end": (15, 0)},
    "צהריים": {"start": (15, 0), "end": (23, 0)},
    "לילה": {"start": (23, 0), "end": (7, 0)},
    "כפולה בוקר": {"start": (7, 0), "end": (19, 0)},
    "כפולה לילה": {"start": (19, 0), "end": (7, 0)},
}

# Aliases for reverse order input
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
WAITING_FOR_REMOVE = 3

pending = {}
pending_confirmations = {}


HELP_TEXT = (
    "👷 *ברוך הבא לבוט תזכורות Inspector!*\n\n"
    "הבוט ישלח לך תזכורת 5 דקות לפני תחילת וסיום כל משמרת.\n"
    "אם לא תאשר — הוא ינודניק כל 2.5 דקות 😄\n\n"
    "📋 *פקודות זמינות:*\n\n"
    "/set\\_shifts — הגדרת משמרות לשבוע\n"
    "/update — עדכון או הוספת משמרת ליום ספציפי\n"
    "/remove — הסרת משמרת מיום ספציפי\n"
    "/list\\_shifts — צפייה בכל המשמרות המוגדרות\n"
    "/test — בדיקת הזרימה (תזכורת לדוגמה)\n"
    "/help — הצגת הודעה זו מחדש\n\n"
    "📝 *סוגי משמרות:*\n"
    "בוקר | צהריים | לילה | כפולה בוקר | כפולה לילה\n\n"
    "📅 *ימים:* ראשון שני שלישי רביעי חמישי שישי שבת"
)


def normalize_shift(text: str):
    """Normalize shift name including aliases."""
    text = text.strip()
    if text in SHIFT_ALIASES:
        return SHIFT_ALIASES[text]
    if text in SHIFTS:
        return text
    return None


def parse_shifts_from_text(text: str):
    """Parse shifts from flexible natural language input, supports multiline."""
    results = []
    # Split by newlines first, then parse each line
    lines = text.replace("\r", "\n").split("\n")
    for line in lines:
        line = line.replace(":", " ").strip()
        if not line:
            continue
        tokens = line.split()
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token in DAY_MAP:
                day_str = token
                shift_str = None
                # Try 2-word shift first
                if i + 2 < len(tokens):
                    candidate2 = tokens[i+1] + " " + tokens[i+2]
                    normalized = normalize_shift(candidate2)
                    if normalized:
                        shift_str = normalized
                        i += 3
                        results.append((day_str, shift_str, DAY_MAP[day_str]))
                        continue
                # Try 1-word shift
                if i + 1 < len(tokens):
                    candidate1 = tokens[i+1]
                    normalized = normalize_shift(candidate1)
                    if normalized:
                        shift_str = normalized
                        i += 2
                        results.append((day_str, shift_str, DAY_MAP[day_str]))
                        continue
                i += 1
            else:
                i += 1
    return results


def parse_single_shift(text: str):
    """Parse a single day+shift from text."""
    results = parse_shifts_from_text(text)
    if results:
        return results[0]
    return None


def _schedule_shift_reminder(context, weekday, time_tuple, shift_name, action, job_name):
    hour, minute = time_tuple
    now = datetime.now(TZ)

    # Calculate reminder time (5 min before)
    reminder_hour = hour
    reminder_minute = minute - 5
    if reminder_minute < 0:
        reminder_minute += 60
        reminder_hour -= 1
        if reminder_hour < 0:
            reminder_hour += 24

    days_ahead = weekday - now.weekday()
    if days_ahead < 0:
        days_ahead += 7

    target = now + timedelta(days=days_ahead)
    target = target.replace(hour=reminder_hour, minute=reminder_minute, second=0, microsecond=0)

    if target < now:
        target += timedelta(weeks=1)

    context.job_queue.run_once(
        send_reminder,
        when=target,
        data={"action": action, "shift": shift_name, "job_name": job_name},
        name=job_name,
        user_id=USER_ID,
        chat_id=USER_ID
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


# ── SET SHIFTS (conversation) ──────────────────────────────────────────────

async def set_shifts_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        return ConversationHandler.END
    await update.message.reply_text(
        "📅 שלח לי את המשמרות שלך לשבוע.\n\n"
        "אפשר בשורה אחת:\n"
        "`ראשון בוקר שני לילה שישי כפולה בוקר`\n\n"
        "או שורה אחרי שורה:\n"
        "`ראשון בוקר`\n`שני לילה`\n`שישי כפולה בוקר`",
        parse_mode="Markdown"
    )
    return WAITING_FOR_SHIFTS


async def set_shifts_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    parsed = parse_shifts_from_text(text)

    if not parsed:
        await update.message.reply_text(
            "לא הצלחתי להבין את המשמרות 😅\nנסה שוב או שלח /help לעזרה."
        )
        return WAITING_FOR_SHIFTS

    # Remove existing shift jobs
    for job in context.job_queue.jobs():
        if job.name.startswith("shift_"):
            job.schedule_removal()

    scheduled = []
    for day_str, shift_str, day_num in parsed:
        shift = SHIFTS[shift_str]
        _schedule_shift_reminder(context, day_num, shift["start"], shift_str, "כניסה", f"shift_start_{day_str}")
        _schedule_shift_reminder(context, day_num, shift["end"], shift_str, "יציאה", f"shift_end_{day_str}")
        scheduled.append(f"📌 {day_str}: {shift_str}")

    msg = "✅ המשמרות הוגדרו:\n\n" + "\n".join(scheduled)
    await update.message.reply_text(msg)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ביטול ✅")
    return ConversationHandler.END


# ── UPDATE SHIFT ────────────────────────────────────────────────────────────

async def update_shift_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        return ConversationHandler.END
    await update.message.reply_text(
        "✏️ איזו משמרת לעדכן או להוסיף?\n\n"
        "שלח לי יום ומשמרת, לדוגמה:\n"
        "`חמישי כפולה בוקר`",
        parse_mode="Markdown"
    )
    return WAITING_FOR_UPDATE


async def update_shift_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    parsed = parse_single_shift(text)

    if not parsed:
        await update.message.reply_text("לא הבנתי 😅 נסה שוב, לדוגמה: `חמישי בוקר`", parse_mode="Markdown")
        return WAITING_FOR_UPDATE

    day_str, shift_str, day_num = parsed
    context.user_data["pending_update"] = (day_str, shift_str, day_num)

    keyboard = [
        [
            InlineKeyboardButton("✅ כן, אני בטוח", callback_data="confirm_update"),
            InlineKeyboardButton("❌ ביטול", callback_data="cancel_action")
        ]
    ]
    await update.message.reply_text(
        f"האם אתה בטוח שאתה רוצה להוסיף/לעדכן את משמרת *{day_str} {shift_str}*?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END


# ── REMOVE SHIFT ────────────────────────────────────────────────────────────

async def remove_shift_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        return ConversationHandler.END

    # Show current shifts as options
    jobs = [j for j in context.job_queue.jobs() if j.name.startswith("shift_start_")]
    if not jobs:
        await update.message.reply_text("אין משמרות מוגדרות כרגע.")
        return ConversationHandler.END

    await update.message.reply_text(
        "🗑 איזו משמרת להסיר?\n\nשלח לי את היום, לדוגמה:\n`ראשון`",
        parse_mode="Markdown"
    )
    return WAITING_FOR_REMOVE


async def remove_shift_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    day_str = text.split()[0] if text else ""

    if day_str not in DAY_MAP:
        await update.message.reply_text("לא הכרתי את היום 😅 נסה שוב, לדוגמה: `ראשון`", parse_mode="Markdown")
        return WAITING_FOR_REMOVE

    # Find the shift for this day
    start_job = next((j for j in context.job_queue.jobs() if j.name == f"shift_start_{day_str}"), None)
    if not start_job:
        await update.message.reply_text(f"לא נמצאה משמרת ביום {day_str}.")
        return ConversationHandler.END

    shift_name = start_job.data["shift"]
    context.user_data["pending_remove"] = day_str

    keyboard = [
        [
            InlineKeyboardButton("✅ כן, אני בטוח", callback_data="confirm_remove"),
            InlineKeyboardButton("❌ ביטול", callback_data="cancel_action")
        ]
    ]
    await update.message.reply_text(
        f"האם אתה בטוח שאתה רוצה להסיר את משמרת *{day_str} {shift_name}*?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END


# ── LIST SHIFTS ─────────────────────────────────────────────────────────────

async def list_shifts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jobs = [j for j in context.job_queue.jobs() if j.name.startswith("shift_start_")]
    if not jobs:
        await update.message.reply_text("אין משמרות מוגדרות כרגע.")
        return

    jobs_sorted = sorted(jobs, key=lambda j: j.next_t)
    msg = "📅 *המשמרות המוגדרות:*\n\n"
    for job in jobs_sorted:
        shift_name = job.data["shift"]
        next_time = job.next_t.astimezone(TZ)
        date_str = next_time.strftime("%d/%m")
        day_name = DAY_NUM_TO_HE.get(next_time.weekday(), "")
        msg += f"📌 {day_name} {date_str} — {shift_name}\n"

    await update.message.reply_text(msg, parse_mode="Markdown")


# ── REMINDERS ───────────────────────────────────────────────────────────────

async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    action = job.data["action"]
    shift = job.data["shift"]
    job_name = job.data["job_name"]

    pending[job_name] = False
    emoji = "🟢" if action == "כניסה" else "🔴"

    keyboard = [[InlineKeyboardButton(f"✅ סימנתי {action}!", callback_data=f"confirm_{job_name}")]]

    await context.bot.send_message(
        chat_id=USER_ID,
        text=f"{emoji} תזכורת! עוד 5 דקות צריך לסמן *{action}* למשמרת {shift} ב-Inspector",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ סימנתי {action}!", callback_data=f"confirm_{job_name}")]])
    )

    context.job_queue.run_once(
        nudge_reminder,
        when=timedelta(seconds=150),  # 2.5 minutes
        data={"action": action, "shift": shift, "job_name": job_name, "count": 1},
        name=f"nudge_{job_name}_1",
        chat_id=USER_ID,
        user_id=USER_ID
    )


async def nudge_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    job_name = job.data["job_name"]
    action = job.data["action"]
    shift = job.data["shift"]
    count = job.data["count"]

    if pending.get(job_name):
        return

    await context.bot.send_message(
        chat_id=USER_ID,
        text=f"⚠️ עוד לא סימנת *{action}* למשמרת {shift}! אל תשכח!",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ סימנתי {action}!", callback_data=f"confirm_{job_name}")]])
    )

    next_count = count + 1
    context.job_queue.run_once(
        nudge_reminder,
        when=timedelta(seconds=150),  # 2.5 minutes
        data={"action": action, "shift": shift, "job_name": job_name, "count": next_count},
        name=f"nudge_{job_name}_{next_count}",
        chat_id=USER_ID,
        user_id=USER_ID
    )


# ── TEST ─────────────────────────────────────────────────────────────────────

async def test_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        return

    job_name = "test_job"
    pending[job_name] = False

    await update.message.reply_text(
        "🧪 *זוהי הודעת טסט!*\n\n"
        "🟢 תזכורת! עוד 5 דקות צריך לסמן *כניסה* למשמרת בוקר ב-Inspector\n\n"
        "לחץ על הכפתור לאישור — אחרת הבוט ינודניק כל 2.5 דקות 😄",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ סימנתי כניסה!", callback_data=f"confirm_{job_name}")]])
    )

    context.job_queue.run_once(
        nudge_reminder,
        when=timedelta(seconds=10),
        data={"action": "כניסה", "shift": "בוקר (טסט)", "job_name": job_name, "count": 1},
        name=f"nudge_{job_name}_1",
        chat_id=USER_ID,
        user_id=USER_ID
    )


# ── CALLBACKS ────────────────────────────────────────────────────────────────

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # Confirm shift reminder
    if data.startswith("confirm_") and not data in ("confirm_update", "confirm_remove"):
        job_name = data.replace("confirm_", "")
        pending[job_name] = True
        for job in context.job_queue.jobs():
            if job.name.startswith(f"nudge_{job_name}"):
                job.schedule_removal()
        await query.edit_message_text(
            text=query.message.text + "\n\n✅ *מעולה! סומן בהצלחה!*",
            parse_mode="Markdown"
        )

    # Confirm update
    elif data == "confirm_update":
        pending_update = context.user_data.get("pending_update")
        if not pending_update:
            await query.edit_message_text("משהו השתבש, נסה שוב.")
            return
        day_str, shift_str, day_num = pending_update
        shift = SHIFTS[shift_str]
        # Remove existing jobs for this day
        for job in context.job_queue.jobs():
            if job.name in (f"shift_start_{day_str}", f"shift_end_{day_str}"):
                job.schedule_removal()
        _schedule_shift_reminder(context, day_num, shift["start"], shift_str, "כניסה", f"shift_start_{day_str}")
        _schedule_shift_reminder(context, day_num, shift["end"], shift_str, "יציאה", f"shift_end_{day_str}")
        await query.edit_message_text(f"✅ משמרת *{day_str} {shift_str}* עודכנה!", parse_mode="Markdown")

    # Confirm remove
    elif data == "confirm_remove":
        day_str = context.user_data.get("pending_remove")
        if not day_str:
            await query.edit_message_text("משהו השתבש, נסה שוב.")
            return
        removed = False
        for job in context.job_queue.jobs():
            if job.name in (f"shift_start_{day_str}", f"shift_end_{day_str}"):
                job.schedule_removal()
                removed = True
        if removed:
            await query.edit_message_text(f"✅ משמרת יום *{day_str}* הוסרה!", parse_mode="Markdown")
        else:
            await query.edit_message_text(f"לא נמצאה משמרת ביום {day_str}.")

    # Cancel
    elif data == "cancel_action":
        await query.edit_message_text("❌ הפעולה בוטלה.")


# ── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TOKEN).build()

    set_shifts_conv = ConversationHandler(
        entry_points=[CommandHandler("set_shifts", set_shifts_start)],
        states={WAITING_FOR_SHIFTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_shifts_receive)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    update_conv = ConversationHandler(
        entry_points=[CommandHandler("update", update_shift_start)],
        states={WAITING_FOR_UPDATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, update_shift_receive)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    remove_conv = ConversationHandler(
        entry_points=[CommandHandler("remove", remove_shift_start)],
        states={WAITING_FOR_REMOVE: [MessageHandler(filters.TEXT & ~filters.COMMAND, remove_shift_receive)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("list_shifts", list_shifts))
    app.add_handler(CommandHandler("test", test_reminder))
    app.add_handler(set_shifts_conv)
    app.add_handler(update_conv)
    app.add_handler(remove_conv)
    app.add_handler(CallbackQueryHandler(button_callback))

    app.run_polling()


if __name__ == "__main__":
    main()
