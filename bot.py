import os
import logging
from datetime import datetime, timedelta
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
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

# Store pending confirmations: job_name -> confirmed bool
pending = {}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "שלום! אני הבוט שיזכיר לך לסמן כניסה ויציאה ב-Inspector 👷\n\n"
        "כדי להגדיר משמרות לשבוע השתמש בפקודה:\n"
        "/set_shifts\n\n"
        "לדוגמה:\n"
        "/set_shifts ראשון:בוקר שני:צהריים שלישי:לילה"
    )


def parse_shifts_from_text(text: str):
    """Parse shifts from flexible natural language input."""
    day_map = {
        "ראשון": 6, "שני": 0, "שלישי": 1,
        "רביעי": 2, "חמישי": 3, "שישי": 4, "שבת": 5
    }
    # Normalize: remove colons, extra spaces
    text = text.replace(":", " ").replace("_", " ")
    tokens = text.split()

    results = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in day_map:
            day_str = token
            # Collect next 1-2 tokens as shift name
            shift_str = None
            if i + 2 < len(tokens):
                candidate2 = tokens[i+1] + " " + tokens[i+2]
                if candidate2 in SHIFTS:
                    shift_str = candidate2
                    i += 3
                    results.append((day_str, shift_str, day_map[day_str]))
                    continue
            if i + 1 < len(tokens):
                candidate1 = tokens[i+1]
                if candidate1 in SHIFTS:
                    shift_str = candidate1
                    i += 2
                    results.append((day_str, shift_str, day_map[day_str]))
                    continue
            i += 1
        else:
            i += 1
    return results


async def set_shifts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        await update.message.reply_text("אין לך הרשאה להשתמש בבוט זה.")
        return

    if not context.args:
        await update.message.reply_text(
            "שלח את המשמרות שלך בפורמט חופשי, לדוגמה:\n"
            "/set_shifts ראשון בוקר שני לילה שישי כפולה בוקר\n\n"
            "סוגי משמרות אפשריים:\n"
            "בוקר | צהריים | לילה | כפולה בוקר | כפולה לילה"
        )
        return

    text = " ".join(context.args)
    parsed = parse_shifts_from_text(text)

    if not parsed:
        await update.message.reply_text(
            "לא הצלחתי להבין את המשמרות 😅\n"
            "נסה לדוגמה:\n"
            "/set_shifts ראשון בוקר שני לילה שישי כפולה בוקר"
        )
        return

    # Remove existing shift jobs
    for job in context.job_queue.jobs():
        if job.name.startswith("shift_"):
            job.schedule_removal()

    scheduled = []
    for day_str, shift_str, day_num in parsed:
        shift = SHIFTS[shift_str]
        _schedule_shift_reminder(
            context, day_num, shift["start"], shift_str, "כניסה", f"shift_start_{day_str}"
        )
        _schedule_shift_reminder(
            context, day_num, shift["end"], shift_str, "יציאה", f"shift_end_{day_str}"
        )
        scheduled.append(f"{day_str}: {shift_str}")

    msg = "✅ המשמרות הוגדרו:\n" + "\n".join(scheduled)
    await update.message.reply_text(msg)


def _schedule_shift_reminder(context, weekday, time_tuple, shift_name, action, job_name):
    hour, minute = time_tuple
    # Subtract 5 minutes
    reminder_dt = datetime.now(TZ).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    ) - timedelta(minutes=5)

    # Find next occurrence of this weekday
    now = datetime.now(TZ)
    days_ahead = weekday - now.weekday()
    if days_ahead < 0:
        days_ahead += 7

    target = now + timedelta(days=days_ahead)
    target = target.replace(
        hour=reminder_dt.hour,
        minute=reminder_dt.minute,
        second=0, microsecond=0
    )

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


async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    action = job.data["action"]
    shift = job.data["shift"]
    job_name = job.data["job_name"]

    pending[job_name] = False

    emoji = "🟢" if action == "כניסה" else "🔴"
    keyboard = [[InlineKeyboardButton(
        f"✅ סימנתי {action}!", callback_data=f"confirm_{job_name}"
    )]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(
        chat_id=USER_ID,
        text=f"{emoji} תזכורת! עוד 5 דקות צריך לסמן *{action}* למשמרת {shift} ב-Inspector",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

    # Schedule nudge after 10 minutes if not confirmed
    context.job_queue.run_once(
        nudge_reminder,
        when=timedelta(minutes=10),
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
        return  # Already confirmed

    keyboard = [[InlineKeyboardButton(
        f"✅ סימנתי {action}!", callback_data=f"confirm_{job_name}"
    )]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(
        chat_id=USER_ID,
        text=f"⚠️ עוד לא סימנת *{action}* למשמרת {shift}! אל תשכח!",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

    # Keep nudging every 10 minutes
    next_count = count + 1
    context.job_queue.run_once(
        nudge_reminder,
        when=timedelta(minutes=10),
        data={"action": action, "shift": shift, "job_name": job_name, "count": next_count},
        name=f"nudge_{job_name}_{next_count}",
        chat_id=USER_ID,
        user_id=USER_ID
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    job_name = query.data.replace("confirm_", "")
    pending[job_name] = True

    # Cancel nudge jobs
    for job in context.job_queue.jobs():
        if job.name.startswith(f"nudge_{job_name}"):
            job.schedule_removal()

    await query.edit_message_text(
        text=query.message.text + "\n\n✅ *מעולה! סומן בהצלחה!*",
        parse_mode="Markdown"
    )


async def list_shifts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jobs = [j for j in context.job_queue.jobs() if j.name.startswith("shift_start_")]
    if not jobs:
        await update.message.reply_text("אין משמרות מוגדרות כרגע.")
        return

    # Sort by next run time
    jobs_sorted = sorted(jobs, key=lambda j: j.next_t)

    msg = "📅 המשמרות המוגדרות:\n\n"
    for job in jobs_sorted:
        shift_name = job.data["shift"]
        next_time = job.next_t.astimezone(TZ)
        day_date = next_time.strftime("%A %d/%m").replace(
            "Sunday", "ראשון").replace("Monday", "שני").replace(
            "Tuesday", "שלישי").replace("Wednesday", "רביעי").replace(
            "Thursday", "חמישי").replace("Friday", "שישי").replace(
            "Saturday", "שבת")
        msg += f"📌 {day_date} — {shift_name}\n"

    await update.message.reply_text(msg)


async def test_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        return

    job_name = "test_job"
    pending[job_name] = False

    keyboard = [[InlineKeyboardButton("✅ סימנתי כניסה!", callback_data=f"confirm_{job_name}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "🧪 *זוהי הודעת טסט!*\n\n"
        "🟢 תזכורת! עוד 5 דקות צריך לסמן *כניסה* למשמרת בוקר ב-Inspector\n\n"
        "לחץ על הכפתור כדי לאשר — אחרת הבוט ימשיך לנודניק כל 10 דקות 😄",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

    # Schedule a nudge after 10 seconds for demo
    context.job_queue.run_once(
        nudge_reminder,
        when=timedelta(seconds=10),
        data={"action": "כניסה", "shift": "בוקר (טסט)", "job_name": job_name, "count": 1},
        name=f"nudge_{job_name}_1",
        chat_id=USER_ID,
        user_id=USER_ID
    )


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("set_shifts", set_shifts))
    app.add_handler(CommandHandler("list_shifts", list_shifts))
    app.add_handler(CommandHandler("test", test_reminder))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.run_polling()


if __name__ == "__main__":
    main()
