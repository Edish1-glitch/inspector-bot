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


async def set_shifts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != USER_ID:
        await update.message.reply_text("אין לך הרשאה להשתמש בבוט זה.")
        return

    if not context.args:
        await update.message.reply_text(
            "שלח את המשמרות שלך בפורמט:\n"
            "/set_shifts ראשון:בוקר שני:צהריים שישי:כפולה_בוקר\n\n"
            "סוגי משמרות אפשריים:\n"
            "בוקר | צהריים | לילה | כפולה_בוקר | כפולה_לילה"
        )
        return

    day_map = {
        "ראשון": 6, "שני": 0, "שלישי": 1,
        "רביעי": 2, "חמישי": 3, "שישי": 4, "שבת": 5
    }

    # Remove existing shift jobs
    current_jobs = context.job_queue.jobs()
    for job in current_jobs:
        if job.name.startswith("shift_"):
            job.schedule_removal()

    scheduled = []
    errors = []

    for arg in context.args:
        try:
            day_str, shift_str = arg.split(":")
            shift_str = shift_str.replace("_", " ")
            day_num = day_map.get(day_str)
            shift = SHIFTS.get(shift_str)

            if day_num is None:
                errors.append(f"יום לא מוכר: {day_str}")
                continue
            if shift is None:
                errors.append(f"משמרת לא מוכרת: {shift_str}")
                continue

            # Schedule start reminder (5 min before)
            _schedule_shift_reminder(
                context, day_num, shift["start"], shift_str, "כניסה", f"shift_start_{day_str}"
            )
            # Schedule end reminder (5 min before)
            _schedule_shift_reminder(
                context, day_num, shift["end"], shift_str, "יציאה", f"shift_end_{day_str}"
            )

            scheduled.append(f"{day_str}: {shift_str}")

        except Exception as e:
            errors.append(f"שגיאה ב-{arg}: {e}")

    msg = "✅ המשמרות הוגדרו:\n" + "\n".join(scheduled)
    if errors:
        msg += "\n\n⚠️ שגיאות:\n" + "\n".join(errors)

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
    jobs = [j for j in context.job_queue.jobs() if j.name.startswith("shift_")]
    if not jobs:
        await update.message.reply_text("אין משמרות מוגדרות כרגע.")
        return

    msg = "📅 המשמרות המוגדרות:\n"
    for job in jobs:
        msg += f"• {job.name} - {job.next_t.astimezone(TZ).strftime('%d/%m %H:%M')}\n"
    await update.message.reply_text(msg)


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("set_shifts", set_shifts))
    app.add_handler(CommandHandler("list_shifts", list_shifts))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.run_polling()


if __name__ == "__main__":
    main()
