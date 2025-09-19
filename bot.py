## TÍCH HỢP UVLOOP ĐỂ TĂNG TỐC ##
import uvloop
uvloop.install()
##################################

import os
import logging
import asyncio
import httpx
import aiofiles
import uuid
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

# --- Cấu hình ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
PIXELDRAIN_API_KEY = os.getenv("PIXELDRAIN_API_KEY")

if not all([API_ID, API_HASH, BOT_TOKEN, PIXELDRAIN_API_KEY]):
    raise ValueError("Một hoặc nhiều biến môi trường chưa được thiết lập.")
API_ID = int(API_ID)

app = Client("my_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Quản lý Trạng thái ---
USER_BATCHES = {}
PENDING_LISTS = {}
BATCH_TIMEOUT = 3.5

# --- Các hàm API ---
async def upload_file(message: Message) -> str:
    """
    Hàm upload file bằng phương pháp tải về đĩa rồi upload.
    Trả về file ID nếu thành công, hoặc chuỗi lỗi nếu thất bại.
    """
    file_path = None
    try:
        logger.info(f"Downloading file from message {message.id}...")
        file_path = await message.download()
        logger.info(f"File downloaded to: {file_path}")

        file_name = os.path.basename(file_path)
        auth_details = ('', PIXELDRAIN_API_KEY)
        url = f"https://pixeldrain.com/api/file/{file_name}"

        logger.info(f"Uploading {file_name} to Pixeldrain...")
        async with aiofiles.open(file_path, "rb") as f:
            async with httpx.AsyncClient() as client:
                response = await client.put(url, content=f, auth=auth_details, timeout=None)
        
        response.raise_for_status()
        response_data = response.json()
        file_id = response_data.get("id")

        if file_id:
            logger.info(f"Successfully uploaded {file_name}, got ID: {file_id}")
            return file_id
        else:
            logger.warning(f"Upload of {file_name} succeeded but API returned no ID.")
            return f"Lỗi: API không trả về ID cho file {file_name}"
            
    except Exception as e:
        logger.error(f"Error processing message {message.id}: {e}", exc_info=True)
        return f"Lỗi nghiêm trọng với file từ tin nhắn {message.id}: {type(e).__name__}"
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            logger.info(f"Cleaned up temporary file: {file_path}")

async def create_pixeldrain_list(file_ids: list[str]) -> str:
    auth_details = ('', PIXELDRAIN_API_KEY)
    url = "https://pixeldrain.com/api/list"
    payload = {"files": [{"id": file_id} for file_id in file_ids]}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, auth=auth_details, timeout=30.0)
        response.raise_for_status()
        response_data = response.json()
        list_id = response_data.get("id")
        if list_id: return list_id
        else: return "Lỗi: API không trả về ID cho list"
    except Exception as e:
        return f"Lỗi nghiêm trọng khi tạo list: {type(e).__name__}"


# --- Logic Xử lý Lô file ---
async def process_file_batch(user_id: int):
    await asyncio.sleep(BATCH_TIMEOUT)
    
    user_data = USER_BATCHES.pop(user_id, None)
    if not user_data or not user_data["messages"]:
        return

    messages_to_process = user_data["messages"]
    
    if len(messages_to_process) == 1:
        message = messages_to_process[0]
        processing_message = await message.reply_text("Đã nhận 1 file, đang xử lý...")
        result_id = await upload_file(message)
        if not result_id.startswith("Lỗi"):
            response_text = f"✅ **Upload thành công!**\n\n🔗 Link: https://pixeldrain.com/u/{result_id}"
        else:
            response_text = f"❌ **Upload thất bại!**\n\n📄 Chi tiết: {result_id}"
        await processing_message.edit_text(response_text, disable_web_page_preview=True)
        return

    count = len(messages_to_process)
    processing_message = await app.send_message(user_id, f"Đã nhận {count} files, đang xử lý đồng thời...")
    
    upload_tasks = [upload_file(msg) for msg in messages_to_process]
    results = await asyncio.gather(*upload_tasks)

    successful_ids = [res for res in results if not res.startswith("Lỗi")]
    failed_uploads = [res for res in results if res.startswith("Lỗi")]

    response_text = f"✅ **Hoàn tất upload {len(successful_ids)}/{count} files!**\n\n"
    if successful_ids:
        response_text += "**Link các file:**\n" + "\n".join(f"🔗 https://pixeldrain.com/u/{fid}" for fid in successful_ids) + "\n\n"
    if failed_uploads:
        response_text += "❌ **Các file thất bại:**\n" + "\n".join(f"📄 {error}" for error in failed_uploads) + "\n\n"

    if successful_ids:
        batch_id = str(uuid.uuid4())
        PENDING_LISTS[batch_id] = successful_ids
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Tạo Album", callback_data=f"create_{batch_id}"),
            InlineKeyboardButton("❌ Hủy", callback_data=f"cancel_{batch_id}")
        ]])
        await processing_message.edit_text(response_text, reply_markup=keyboard)
    else:
        await processing_message.edit_text(response_text)


# --- Handlers và các hàm Callback ---
@app.on_message(filters.command("start"))
async def start_handler(_, message: Message):
    await message.reply_text("Xin chào! Gửi file cho tôi để bắt đầu.")

@app.on_message(filters.document | filters.photo | filters.video | filters.audio)
async def main_file_handler(_, message: Message):
    user_id = message.from_user.id
    
    if user_id not in USER_BATCHES:
        USER_BATCHES[user_id] = {"messages": [], "task": None}

    if USER_BATCHES[user_id]["task"]:
        USER_BATCHES[user_id]["task"].cancel()

    USER_BATCHES[user_id]["messages"].append(message)

    USER_BATCHES[user_id]["task"] = asyncio.create_task(process_file_batch(user_id))

@app.on_callback_query(filters.regex("^create_"))
async def create_list_callback(_, callback_query):
    batch_id = callback_query.data.split("_")[1]
    file_ids = PENDING_LISTS.pop(batch_id, None)
    if not file_ids:
        await callback_query.answer("Lô file này đã hết hạn hoặc không tồn tại.", show_alert=True)
        return
    await callback_query.message.edit_text("Đang tạo link album...")
    list_result = await create_pixeldrain_list(file_ids)
    if not list_result.startswith("Lỗi"):
        response_text = f"✅ **Tạo album thành công!**\n\n🔗 Link album của bạn: https://pixeldrain.com/l/{list_result}"
    else:
        response_text = f"❌ **Tạo album thất bại:** {list_result}"
    await callback_query.message.edit_text(response_text)
    await callback_query.answer("Đã tạo album!")


@app.on_callback_query(filters.regex("^cancel_"))
async def cancel_list_callback(_, callback_query):
    batch_id = callback_query.data.split("_")[1]
    PENDING_LISTS.pop(batch_id, None)
    original_text_parts = callback_query.message.text.markdown.split("\n\n")
    cleaned_text = "\n\n".join(original_text_parts[:-1]) 
    await callback_query.message.edit_text(f"{cleaned_text}\n\n*Đã hủy thao tác tạo album.*")
    await callback_query.answer("Đã hủy.")


# --- Chạy bot ---
if __name__ == "__main__":
    logger.info("Bot đang khởi động với uvloop và logic ổn định...")
    app.run()
    logger.info("Bot đã dừng.")
