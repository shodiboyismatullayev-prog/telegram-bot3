import os
import io
import logging
import tempfile
import subprocess
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import img2pdf
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import cm
from pdf2docx import Converter
from PIL import Image
import pillow_heif

pillow_heif.register_heif_opener()

# ---------------------------------------------------------------------------
# Sozlamalar
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

# Majburiy a'zolik talab qilinadigan kanal va guruh username'lari (@ bilan)
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "@mychannel")
REQUIRED_GROUP = os.environ.get("REQUIRED_GROUP", "@mygroup")

# Telegram a'zolik holatlari ichidan "a'zo hisoblanadigan" holatlar
MEMBER_STATUSES = {"member", "administrator", "creator"}

# Har bir foydalanuvchi uchun vaqtinchalik holat (rasm to'plash uchun)
# user_id -> list of image file paths
user_images: dict[int, list[str]] = {}

TMP_DIR = Path(tempfile.gettempdir()) / "pdfbot_files"
TMP_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Baza bilan ishlash
# ---------------------------------------------------------------------------

def get_connection():
    return psycopg2.connect(DATABASE_URL)


def init_db() -> None:
    """Bot ishga tushganda jadval yo'q bo'lsa yaratadi."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            joined_at TIMESTAMP DEFAULT NOW(),
            files_converted INT DEFAULT 0
        )
        """
    )
    conn.commit()
    cur.close()
    conn.close()


def register_user(user_id: int, username: str | None) -> None:
    """Foydalanuvchini bazaga qo'shadi (agar mavjud bo'lmasa)."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO users (user_id, username)
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO NOTHING
        """,
        (user_id, username),
    )
    conn.commit()
    cur.close()
    conn.close()


def increment_conversion_count(user_id: int) -> None:
    """Foydalanuvchining konvertatsiya sonini birga oshiradi."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET files_converted = files_converted + 1 WHERE user_id = %s",
        (user_id,),
    )
    conn.commit()
    cur.close()
    conn.close()


def get_total_users() -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    count = cur.fetchone()[0]
    cur.close()
    conn.close()
    return count


# ---------------------------------------------------------------------------
# Yordamchi funksiyalar
# ---------------------------------------------------------------------------

def text_to_pdf(text: str, output_path: str) -> None:
    """Oddiy matnni A4 PDF sahifalariga yozadi (uzun matn uchun sahifalarga bo'lib)."""
    c = canvas.Canvas(output_path, pagesize=A4)
    width, height = A4

    margin = 2 * cm
    max_width = width - 2 * margin
    font_name = "Helvetica"
    font_size = 12
    line_height = font_size * 1.4

    c.setFont(font_name, font_size)

    # Matnni so'zlarga bo'lib, sahifa kengligiga mos qatorlarga yig'amiz
    paragraphs = text.split("\n")
    y = height - margin

    def draw_line(line: str):
        nonlocal y
        if y < margin:
            c.showPage()
            c.setFont(font_name, font_size)
            y = height - margin
        c.drawString(margin, y, line)
        y -= line_height

    for paragraph in paragraphs:
        if paragraph.strip() == "":
            draw_line("")
            continue

        words = paragraph.split(" ")
        current_line = ""
        for word in words:
            test_line = (current_line + " " + word).strip()
            if c.stringWidth(test_line, font_name, font_size) <= max_width:
                current_line = test_line
            else:
                draw_line(current_line)
                current_line = word
        if current_line:
            draw_line(current_line)

    c.save()


def images_to_pdf(image_paths: list[str], output_path: str) -> None:
    with open(output_path, "wb") as f:
        f.write(img2pdf.convert(image_paths))


def pdf_to_word(pdf_path: str, output_path: str) -> None:
    cv = Converter(pdf_path)
    cv.convert(output_path)
    cv.close()


def word_to_pdf(docx_path: str, output_dir: str) -> str:
    """LibreOffice yordamida docx -> pdf. Natija fayl yo'lini qaytaradi."""
    subprocess.run(
        [
            "libreoffice",
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            output_dir,
            docx_path,
        ],
        check=True,
        timeout=120,
    )
    base = Path(docx_path).stem
    return str(Path(output_dir) / f"{base}.pdf")


def convert_image_to_jpg(input_path: str, output_path: str) -> None:
    """Har qanday rasm formatini (HEIC, PNG, WEBP) JPG ga o'tkazadi."""
    img = Image.open(input_path)
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(output_path, "JPEG", quality=90)


def voice_to_mp3(input_path: str, output_path: str) -> None:
    """Telegram voice (.ogg) faylni .mp3 ga o'tkazadi (ffmpeg orqali)."""
    subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, output_path],
        check=True,
        capture_output=True,
        timeout=60,
    )


def main_menu_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("📷 Rasm(lar) → PDF", callback_data="mode_images")],
        [InlineKeyboardButton("📝 Matn → PDF", callback_data="mode_text")],
        [InlineKeyboardButton("📄 PDF → Word", callback_data="mode_pdf2word")],
        [InlineKeyboardButton("📃 Word → PDF", callback_data="mode_word2pdf")],
        [InlineKeyboardButton("🖼 Rasm formatini o'zgartirish", callback_data="mode_imgconvert")],
        [InlineKeyboardButton("🎵 Ovozli xabar → MP3", callback_data="mode_voice2mp3")],
    ]
    return InlineKeyboardMarkup(buttons)


def result_keyboard(repeat_mode: str) -> InlineKeyboardMarkup:
    """Konvertatsiyadan keyin chiqadigan tugmalar: yana bir marta / bosh menyu."""
    buttons = [
        [InlineKeyboardButton("🔄 Yana bir marta", callback_data=repeat_mode)],
        [InlineKeyboardButton("🏠 Bosh menyu", callback_data="go_home")],
    ]
    return InlineKeyboardMarkup(buttons)


def subscription_keyboard() -> InlineKeyboardMarkup:
    """Kanal/guruhga o'tish va a'zolikni qayta tekshirish tugmalari."""
    channel_url = f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}"
    group_url = f"https://t.me/{REQUIRED_GROUP.lstrip('@')}"
    buttons = [
        [InlineKeyboardButton("📢 Kanalga o'tish", url=channel_url)],
        [InlineKeyboardButton("👥 Guruhga o'tish", url=group_url)],
        [InlineKeyboardButton("✅ Tekshirish", callback_data="check_subscription")],
    ]
    return InlineKeyboardMarkup(buttons)


async def is_subscribed(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Foydalanuvchi kanal va guruhga a'zo ekanligini tekshiradi."""
    try:
        channel_member = await context.bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        group_member = await context.bot.get_chat_member(REQUIRED_GROUP, user_id)
    except Exception:
        logger.exception("A'zolikni tekshirishda xatolik (bot admin emasmi?)")
        return False

    return (
        channel_member.status in MEMBER_STATUSES
        and group_member.status in MEMBER_STATUSES
    )


async def send_subscription_required(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "⚠️ Botdan foydalanish uchun avval quyidagilarga a'zo bo'ling:\n\n"
        f"📢 Kanal: {REQUIRED_CHANNEL}\n"
        f"👥 Guruh: {REQUIRED_GROUP}\n\n"
        "A'zo bo'lgach, \"✅ Tekshirish\" tugmasini bosing."
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=subscription_keyboard())
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=subscription_keyboard())


# ---------------------------------------------------------------------------
# Komandalar
# ---------------------------------------------------------------------------

async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bosh menyuni chiqaradi — /start buyrug'idan ham, tugmadan ham chaqirish mumkin."""
    user_images.pop(update.effective_user.id, None)
    context.user_data["mode"] = None
    text = "Salom! 👋 Men fayl konvertatsiya botiman.\n\nQuyidagilardan birini tanlang:"

    if update.message:
        await update.message.reply_text(text, reply_markup=main_menu_keyboard())
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=main_menu_keyboard())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    register_user(update.effective_user.id, update.effective_user.username)

    if not await is_subscribed(update.effective_user.id, context):
        await send_subscription_required(update, context)
        return
    await send_main_menu(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_images.pop(update.effective_user.id, None)
    context.user_data["mode"] = None
    await update.message.reply_text(
        "Bekor qilindi. Yangi amal tanlang:", reply_markup=main_menu_keyboard()
    )


async def done_images(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Foydalanuvchi /done yozganda, to'plangan rasmlarni PDFga aylantiradi."""
    if not await is_subscribed(update.effective_user.id, context):
        await send_subscription_required(update, context)
        return

    user_id = update.effective_user.id
    images = user_images.get(user_id, [])

    if not images:
        await update.message.reply_text(
            "Hali birorta rasm yubormadingiz. Avval rasm(lar) yuboring."
        )
        return

    output_path = str(TMP_DIR / f"{user_id}_images.pdf")
    try:
        images_to_pdf(images, output_path)
        with open(output_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename="rasmlar.pdf",
                caption="Mana PDF faylingiz ✅",
                reply_markup=result_keyboard("mode_images"),
            )
        increment_conversion_count(user_id)
    except Exception as e:
        logger.exception("Rasm->PDF xatosi")
        await update.message.reply_text(f"Xatolik yuz berdi: {e}")
    finally:
        for p in images:
            Path(p).unlink(missing_ok=True)
        Path(output_path).unlink(missing_ok=True)
        user_images.pop(user_id, None)
        context.user_data["mode"] = None


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    total = get_total_users()
    await update.message.reply_text(f"Jami foydalanuvchilar: {total}")


# ---------------------------------------------------------------------------
# Tugma bosilganda (menyu tanlash)
# ---------------------------------------------------------------------------

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    mode = query.data

    if mode == "check_subscription":
        if await is_subscribed(update.effective_user.id, context):
            await send_main_menu(update, context)
        else:
            await query.answer("Hali to'liq a'zo bo'lmadingiz ❌", show_alert=True)
        return

    if not await is_subscribed(update.effective_user.id, context):
        await send_subscription_required(update, context)
        return

    if mode == "go_home":
        await send_main_menu(update, context)
        return

    context.user_data["mode"] = mode
    user_images.pop(update.effective_user.id, None)

    messages = {
        "mode_images": (
            "📷 Rasm(lar) → PDF rejimi tanlandi.\n\n"
            "Endi bir yoki bir nechta rasm yuboring. Hammasini yuborib bo'lgach, "
            "/done buyrug'ini yozing."
        ),
        "mode_text": (
            "📝 Matn → PDF rejimi tanlandi.\n\n"
            "Endi PDFga aylantirmoqchi bo'lgan matnni shu yerga yozing va yuboring."
        ),
        "mode_pdf2word": (
            "📄 PDF → Word rejimi tanlandi.\n\n"
            "Endi .pdf faylni yuboring."
        ),
        "mode_word2pdf": (
            "📃 Word → PDF rejimi tanlandi.\n\n"
            "Endi .docx faylni yuboring."
        ),
        "mode_imgconvert": (
            "🖼 Rasm formatini o'zgartirish rejimi tanlandi.\n\n"
            "Endi rasmni Document (fayl) sifatida yuboring — HEIC, PNG, WEBP "
            "formatlari JPG ga o'tkaziladi."
        ),
        "mode_voice2mp3": (
            "🎵 Ovozli xabar → MP3 rejimi tanlandi.\n\n"
            "Endi ovozli xabar (voice message) yuboring."
        ),
    }

    await query.edit_message_text(messages.get(mode, "Noma'lum tanlov."))


# ---------------------------------------------------------------------------
# Xabar handlerlari
# ---------------------------------------------------------------------------

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_subscribed(update.effective_user.id, context):
        await send_subscription_required(update, context)
        return

    mode = context.user_data.get("mode")
    if mode != "mode_images":
        await update.message.reply_text(
            "Avval menyudan '📷 Rasm(lar) → PDF' ni tanlang. /start"
        )
        return

    user_id = update.effective_user.id
    photo = update.message.photo[-1]
    file = await photo.get_file()

    path = TMP_DIR / f"{user_id}_{len(user_images.get(user_id, []))}.jpg"
    await file.download_to_drive(str(path))

    user_images.setdefault(user_id, []).append(str(path))
    count = len(user_images[user_id])
    await update.message.reply_text(
        f"Rasm qabul qilindi ✅ (jami: {count})\n"
        f"Yana rasm yuborishingiz mumkin, yoki tugatish uchun /done yozing."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_subscribed(update.effective_user.id, context):
        await send_subscription_required(update, context)
        return

    mode = context.user_data.get("mode")
    if mode != "mode_text":
        await update.message.reply_text(
            "Avval menyudan kerakli rejimni tanlang. /start"
        )
        return

    text = update.message.text
    user_id = update.effective_user.id
    output_path = str(TMP_DIR / f"{user_id}_text.pdf")

    try:
        text_to_pdf(text, output_path)
        with open(output_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename="matn.pdf",
                caption="Mana PDF faylingiz ✅",
                reply_markup=result_keyboard("mode_text"),
            )
        increment_conversion_count(user_id)
    except Exception as e:
        logger.exception("Matn->PDF xatosi")
        await update.message.reply_text(f"Xatolik yuz berdi: {e}")
    finally:
        Path(output_path).unlink(missing_ok=True)
        context.user_data["mode"] = None


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_subscribed(update.effective_user.id, context):
        await send_subscription_required(update, context)
        return

    mode = context.user_data.get("mode")
    if mode != "mode_voice2mp3":
        await update.message.reply_text(
            "Avval menyudan '🎵 Ovozli xabar → MP3' ni tanlang. /start"
        )
        return

    user_id = update.effective_user.id
    voice = update.message.voice
    input_path = str(TMP_DIR / f"{user_id}_voice_input.ogg")
    output_path = str(TMP_DIR / f"{user_id}_voice_output.mp3")

    file = await voice.get_file()
    await file.download_to_drive(input_path)

    await update.message.reply_text("Konvertatsiya qilinmoqda, kuting... ⏳")

    try:
        voice_to_mp3(input_path, output_path)
        with open(output_path, "rb") as f:
            await update.message.reply_audio(
                audio=f,
                filename="natija.mp3",
                caption="Mana MP3 fayl ✅",
                reply_markup=result_keyboard("mode_voice2mp3"),
            )
        increment_conversion_count(user_id)
    except Exception as e:
        logger.exception("Voice->MP3 xatosi")
        await update.message.reply_text(f"Xatolik yuz berdi: {e}")
    finally:
        Path(input_path).unlink(missing_ok=True)
        Path(output_path).unlink(missing_ok=True)
        context.user_data["mode"] = None


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_subscribed(update.effective_user.id, context):
        await send_subscription_required(update, context)
        return

    mode = context.user_data.get("mode")
    doc = update.message.document
    user_id = update.effective_user.id

    if mode == "mode_pdf2word":
        if not doc.file_name.lower().endswith(".pdf"):
            await update.message.reply_text("Iltimos, .pdf fayl yuboring.")
            return

        input_path = str(TMP_DIR / f"{user_id}_input.pdf")
        output_path = str(TMP_DIR / f"{user_id}_output.docx")

        file = await doc.get_file()
        await file.download_to_drive(input_path)

        await update.message.reply_text("Konvertatsiya qilinmoqda, kuting... ⏳")

        try:
            pdf_to_word(input_path, output_path)
            with open(output_path, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename="natija.docx",
                    caption="Mana Word fayl ✅",
                    reply_markup=result_keyboard("mode_pdf2word"),
                )
            increment_conversion_count(user_id)
        except Exception as e:
            logger.exception("PDF->Word xatosi")
            await update.message.reply_text(f"Xatolik yuz berdi: {e}")
        finally:
            Path(input_path).unlink(missing_ok=True)
            Path(output_path).unlink(missing_ok=True)
            context.user_data["mode"] = None

    elif mode == "mode_word2pdf":
        if not doc.file_name.lower().endswith((".docx", ".doc")):
            await update.message.reply_text("Iltimos, .docx fayl yuboring.")
            return

        input_path = str(TMP_DIR / f"{user_id}_input.docx")
        file = await doc.get_file()
        await file.download_to_drive(input_path)

        await update.message.reply_text("Konvertatsiya qilinmoqda, kuting... ⏳")

        try:
            output_path = word_to_pdf(input_path, str(TMP_DIR))
            with open(output_path, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename="natija.pdf",
                    caption="Mana PDF fayl ✅",
                    reply_markup=result_keyboard("mode_word2pdf"),
                )
            increment_conversion_count(user_id)
        except Exception as e:
            logger.exception("Word->PDF xatosi")
            await update.message.reply_text(f"Xatolik yuz berdi: {e}")
        finally:
            Path(input_path).unlink(missing_ok=True)
            try:
                Path(output_path).unlink(missing_ok=True)
            except NameError:
                pass
            context.user_data["mode"] = None

    elif mode == "mode_imgconvert":
        input_path = str(TMP_DIR / f"{user_id}_img_input")
        output_path = str(TMP_DIR / f"{user_id}_img_output.jpg")

        file = await doc.get_file()
        await file.download_to_drive(input_path)

        await update.message.reply_text("Konvertatsiya qilinmoqda, kuting... ⏳")

        try:
            convert_image_to_jpg(input_path, output_path)
            with open(output_path, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename="natija.jpg",
                    caption="Mana JPG fayl ✅",
                    reply_markup=result_keyboard("mode_imgconvert"),
                )
            increment_conversion_count(user_id)
        except Exception as e:
            logger.exception("Rasm konvertatsiya xatosi")
            await update.message.reply_text(f"Xatolik yuz berdi: {e}")
        finally:
            Path(input_path).unlink(missing_ok=True)
            Path(output_path).unlink(missing_ok=True)
            context.user_data["mode"] = None

    else:
        await update.message.reply_text(
            "Avval menyudan kerakli rejimni tanlang. /start"
        )


# ---------------------------------------------------------------------------
# Asosiy ishga tushirish
# ---------------------------------------------------------------------------

def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable topilmadi. Uni Railway sozlamalarida qo'shing."
        )

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL environment variable topilmadi. Railway'da PostgreSQL qo'shilganmi?"
        )

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("done", done_images))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot ishga tushdi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
