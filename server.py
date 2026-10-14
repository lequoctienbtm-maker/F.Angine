import os, re, json, time, subprocess, urllib.request, urllib.error, urllib.parse, zipfile, glob, base64, threading, queue, traceback, uuid
from flask import Flask, Response, send_file, request, jsonify, send_from_directory
from flask_sock import Sock

API_KEYS = [
    "You API KEY HERE",
    "You API KEY HERE",
    "You API KEY HERE",
]

VALID_MODELS = [
    "gemini-flash-lite-latest",
    "gemini-flash-latest",
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash"
]

CURRENT_KEY_IDX = 0
CURRENT_MODEL_IDX = 0

BASE_WORKSPACE = "workspaces"
META_SESSIONS_FILE = os.path.join(BASE_WORKSPACE, "sessions.json")
os.makedirs(BASE_WORKSPACE, exist_ok=True)

app = Flask(__name__)
sock = Sock(app)

# Quản lý kết nối & trạng thái từng phiên
ACTIVE_CLIENTS = {}      # ws -> session_id
SESSION_STATES = {}      # session_id -> {is_busy, task_title, phase, think}
TASK_QUEUE = queue.Queue()
LAST_UPLOADED_FILES = {} # session_id -> file_dict
WAITING_CLARIFICATIONS = {} # session_id -> True/False
PENDING_ORIGIN_TASKS = {}   # session_id -> text

def get_session_dir(sid):
    sdir = os.path.join(BASE_WORKSPACE, sid)
    os.makedirs(sdir, exist_ok=True)
    return sdir

def load_sessions_meta():
    if os.path.exists(META_SESSIONS_FILE):
        try:
            with open(META_SESSIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    default_meta = [{"id": "default", "title": "Phiên làm việc chính", "created_at": time.time()}]
    save_sessions_meta(default_meta)
    return default_meta

def save_sessions_meta(meta_list):
    try:
        with open(META_SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(meta_list, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def get_session_history(sid):
    sdir = get_session_dir(sid)
    fpath = os.path.join(sdir, "history.json")
    if os.path.exists(fpath):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_session_history(sid, history):
    sdir = get_session_dir(sid)
    fpath = os.path.join(sdir, "history.json")
    try:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[LỖI LƯU LỊCH SỬ {sid}]:", e)

def get_current_codebase(sid):
    sdir = get_session_dir(sid)
    html_f = os.path.join(sdir, "index.html")
    py_f = os.path.join(sdir, "script.py")
    if os.path.exists(html_f):
        with open(html_f, "r", encoding="utf-8") as f:
            return "HTML", f.read()
    elif os.path.exists(py_f):
        with open(py_f, "r", encoding="utf-8") as f:
            return "PYTHON", f.read()
    return None, ""

def broadcast_to_session(sid, msg_obj, save=True):
    if save:
        history = get_session_history(sid)
        history.append(msg_obj)
        save_session_history(sid, history)

    dead = []
    for client, client_sid in list(ACTIVE_CLIENTS.items()):
        if client_sid == sid:
            try:
                client.send(json.dumps(msg_obj))
            except Exception:
                dead.append(client)
    for d in dead:
        ACTIVE_CLIENTS.pop(d, None)

def update_session_state(sid, is_busy=None, task_title=None, phase=None, think=None):
    st = SESSION_STATES.setdefault(sid, {"is_busy": False, "task_title": "", "phase": "", "think": None})
    if is_busy is not None: st["is_busy"] = is_busy
    if task_title is not None: st["task_title"] = task_title
    if phase is not None: st["phase"] = phase
    if think is not None: st["think"] = think if think != {} else None
    broadcast_to_session(sid, {"t": "state_sync", "state": st}, save=False)

def call_gemini_smart(prompt, image_b64=None, mime_type="image/png", max_tokens=2048, timeout=20):
    global CURRENT_KEY_IDX, CURRENT_MODEL_IDX

    parts = [{"text": prompt}]
    if image_b64:
        parts.append({"inlineData": {"mimeType": mime_type, "data": image_b64}})

    while True:
        key = API_KEYS[CURRENT_KEY_IDX]
        model = VALID_MODELS[CURRENT_MODEL_IDX]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens}
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Linux; Android 10; Termux) Mobile",
                "Connection": "keep-alive"
            }
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                res_json = json.loads(res.read().decode())
                return res_json["candidates"][0]["content"]["parts"][0]["text"]
        except urllib.error.HTTPError as e:
            if e.code in (429, 401, 403):
                CURRENT_KEY_IDX = (CURRENT_KEY_IDX + 1) % len(API_KEYS)
                time.sleep(1 if CURRENT_KEY_IDX != 0 else 4)
            else:
                CURRENT_MODEL_IDX = (CURRENT_MODEL_IDX + 1) % len(VALID_MODELS)
                time.sleep(1)
        except Exception:
            CURRENT_KEY_IDX = (CURRENT_KEY_IDX + 1) % len(API_KEYS)
            CURRENT_MODEL_IDX = (CURRENT_MODEL_IDX + 1) % len(VALID_MODELS)
            time.sleep(1.5)

def generate_ai_image(prompt_text, sid, filename=None):
    try:
        if not filename:
            filename = f"img_{int(time.time())}.png"
        clean_p = urllib.parse.quote(prompt_text.strip())
        img_url = f"https://image.pollinations.ai/prompt/{clean_p}?width=768&height=768&nologo=true"
        req = urllib.request.Request(img_url, headers={"User-Agent": "Mozilla/5.0"})
        save_path = os.path.join(get_session_dir(sid), filename)
        with urllib.request.urlopen(req, timeout=25) as res:
            with open(save_path, "wb") as f:
                f.write(res.read())
        return filename, f"/workspace_file/{sid}/{filename}"
    except Exception as e:
        return None, str(e)

def split_into_subtasks(big_task):
    prompt = (
        "Bạn là Giám đốc Kiến trúc Hệ thống. Phân tích dự án sau thành 3 đến 5 giai đoạn phát triển lũy tiến:\n"
        f"'{big_task}'\n"
        "BẮT BUỘC TRẢ VỀ JSON MẢNG CHUỖI THUẦN: [\"Giai đoạn 1: ...\", \"Giai đoạn 2: ...\"]"
    )
    try:
        res = call_gemini_smart(prompt, max_tokens=1024, timeout=15)
        m = re.search(r"\[.*\]", res, re.S)
        if m:
            arr = json.loads(m.group(0))
            if isinstance(arr, list) and len(arr) > 0:
                return [str(x) for x in arr]
    except Exception:
        pass
    return [big_task]

def parse_json_safe(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try: return json.loads(m.group(0))
            except: pass
    return {"need_clarification": False, "clarified_task": text}

def run_box(sid, code):
    is_web = bool(re.search(r"<!DOCTYPE html|<html|<script", code, re.I))
    sdir = get_session_dir(sid)
    fpath = os.path.join(sdir, "index.html" if is_web else "script.py")

    if is_web:
        fixed_code = code
        if "<script" in fixed_code and fixed_code.count("<script") > fixed_code.count("</script>"):
            fixed_code = fixed_code + "\n</script>"
        if "<body" in fixed_code and "</body>" not in fixed_code:
            fixed_code = fixed_code + "\n</body>"
        if "<html" in fixed_code and "</html>" not in fixed_code:
            fixed_code = fixed_code + "\n</html>"

        with open(fpath, "w", encoding="utf-8") as f:
            f.write(fixed_code)

        errs = []
        if code.count("<script") != code.count("</script>"): errs.append("Thẻ <script> không đóng khớp")
        if code.count("<html") != code.count("</html>"): errs.append("Thẻ <html> không đóng khớp")
        return is_web, ("[CẢNH BÁO HTML]:\n" + "\n".join(errs)) if errs else "[HTML HỢP LỆ]: Cấu trúc chuẩn 100%."

    with open(fpath, "w", encoding="utf-8") as f:
        f.write(code)
    try:
        compile(code, fpath, "exec")
        p = subprocess.run(["python", fpath], capture_output=True, text=True, timeout=5)
        return False, p.stderr.strip() if p.stderr else (p.stdout.strip() or "[Chạy tốt]")
    except Exception as e:
        return False, f"[LỖI]: {e}"

def execute_engine_pipeline(sid, final_task, img_b64=None, mime="image/png"):
    code_type, existing_code = get_current_codebase(sid)
    has_existing = bool(existing_code.strip())

    memory_prompt_part = ""
    if has_existing:
        memory_prompt_part = f"\n\n[MÃ NGUỒN HIỆN TẠI CỦA PHIÊN NÀY]:\n```{existing_code}```\nLƯU Ý: Phải kế thừa 100% mã nguồn này, tích hợp tính năng mới vào kiến trúc cũ."

    # 1. QUẢN LÝ
    update_session_state(sid, phase="Quản lý lập kế hoạch...", think={"s": "manager", "a": "📋", "txt": "Quản lý đang thiết kế kiến trúc..."})
    p_mgr = (
        "Bạn là Giám đốc Kiến trúc Hệ thống. Quy hoạch giải pháp kỹ thuật:\n"
        f"Yêu cầu: '{final_task}'.{memory_prompt_part}\n"
        "Chỉ đạo dứt khoát 2-3 gạch đầu dòng, quy định biến trạng thái và cấu trúc module súc tích."
    )
    mgr_text = call_gemini_smart(p_mgr, img_b64, mime, max_tokens=1024, timeout=15)
    broadcast_to_session(sid, {"s": "manager", "a": "📋", "n": "Quản lý", "b": "Kiến trúc", "txt": mgr_text})

    code = existing_code
    fb = ""

    for r in range(1, 4):
        if r > 1:
            time.sleep(1.5)
            broadcast_to_session(sid, {"s": "sys", "a": "🔄", "n": "Hệ Thống", "b": f"Vòng sửa {r}/3", "txt": f"Phát hiện code chưa đạt chuẩn, đang ép Dev sửa lại (Vòng {r}/3)..."})

        # 2. DEV
        update_session_state(sid, phase=f"Dev đang lập trình (Vòng {r}/3)...", think={"s": "dev", "a": "⚡", "txt": f"Dev đang triển khai mã nguồn (Vòng {r})..."})
        if r == 1:
            p_dev = (
                "Bạn là Senior Developer. Triển khai kế hoạch:\n"
                f"{mgr_text}\nYêu cầu: '{final_task}'.{memory_prompt_part}\n"
                "QUY TẮC BẮT BUỘC:\n"
                "1. Viết code hoàn chỉnh 100% trong một khối ``` duy nhất (HTML/CSS/JS gom trọn vẹn).\n"
                "2. Hỗ trợ thao tác cảm ứng chạm mobile (touch/click), tự quản lý vòng lặp.\n"
                "3. TUYỆT ĐỐI KHÔNG CẮT CỤT: Đóng đủ các thẻ </script>, </body>, </html>."
            )
        else:
            p_dev = (
                f"Bạn là Dev. Checker và Sandbox đã từ chối code vì lỗi:\n\"{fb}\"\n"
                f"Đoạn cuối code bị đứt:\n```{code[-600:]}```\n"
                "BẮT BUỘC: Tối ưu thuật toán gọn hơn, viết lại HOÀN CHỈNH 100% và ĐÓNG ĐỦ </script>, </body>, </html>."
            )

        res_d = call_gemini_smart(p_dev, img_b64, mime, max_tokens=8192, timeout=35)
        m_code = re.search(r"```(?:\w+)?\s*(.*?)\s*```", res_d, re.S)
        code = m_code.group(1).strip() if m_code else res_d.strip()
        chat_d = res_d.replace(m_code.group(0), "").strip() if m_code else f"Đã hoàn thành mã nguồn (Vòng {r})."
        broadcast_to_session(sid, {"s": "dev", "a": "⚡", "n": "Dev", "b": f"Coder (Vòng {r})", "txt": chat_d, "code": code})

        # 3. TESTING
        update_session_state(sid, phase=f"Kiểm thử Sandbox (Vòng {r})...", think={"s": "tester", "a": "🧪", "txt": f"Testing đang chạy thử Sandbox (Vòng {r})..."})
        is_web, log = run_box(sid, code)
        broadcast_to_session(sid, {"s": "tester", "a": "🧪", "n": "Testing", "b": "Kiểm thử", "txt": f"Kết quả kiểm thử môi trường ({'Web' if is_web else 'Python'}):", "log": log})

        # 4. CHECKER
        update_session_state(sid, phase=f"Checker nghiệm thu (Vòng {r})...", think={"s": "checker", "a": "🛡️", "txt": "Checker đang nghiệm thu..."})
        p_chk = (
            f"Bạn là Checker QA. Task: '{final_task}'.\n"
            f"Đoạn cuối code Dev:\n```{code[-500:]}```\nLog Sandbox:\n{log}\n"
            "QUY TẮC:\n"
            "- Nếu thiếu thẻ đóng hoặc code dang dở: DÒNG ĐẦU ghi 'STATUS: REVISE'.\n"
            "- Nếu hoàn chỉnh đạt chuẩn: DÒNG ĐẦU ghi 'STATUS: APPROVED'.\n"
            "- Các dòng sau ghi nhận xét ngắn."
        )
        res_c = call_gemini_smart(p_chk, max_tokens=1024, timeout=15)
        lines = [l.strip() for l in res_c.strip().split("\n") if l.strip()]

        st = "REVISE"
        if lines and "APPROVED" in lines[0].upper():
            st = "APPROVED"

        fb = "\n".join(lines[1:]).strip() if len(lines) > 1 else res_c
        broadcast_to_session(sid, {"s": "checker", "a": "🛡️", "n": "Checker", "b": "Nghiệm thu", "txt": fb, "st": st})

        if st == "APPROVED":
            break

def background_worker():
    while True:
        item = TASK_QUEUE.get()
        sid = item["session_id"]
        try:
            update_session_state(sid, is_busy=True, task_title=item.get("task", item.get("text", "")))
            itype = item["type"]
            if itype == "image":
                user_text = item["text"]
                update_session_state(sid, phase="Artist vẽ ảnh...", think={"s": "artist", "a": "🎨", "txt": "Artist AI đang phác họa hình ảnh..."})
                p_trans = f"Dịch và mô tả chi tiết hình ảnh sau thành 1 câu prompt tiếng Anh: '{user_text}'. Chỉ trả về 1 câu prompt."
                prompt_en = call_gemini_smart(p_trans, max_tokens=512, timeout=15).strip()
                f_name, img_url = generate_ai_image(prompt_en, sid)
                if f_name:
                    broadcast_to_session(sid, {
                        "s": "artist", "a": "🎨", "n": "Artist AI", "b": "Họa sĩ",
                        "txt": f"Đã vẽ hoàn thành: '{user_text}'\nPrompt AI: {prompt_en}",
                        "img_url": img_url
                    })
                else:
                    broadcast_to_session(sid, {"s": "artist", "a": "⚠️", "n": "Artist AI", "b": "Lỗi vẽ", "txt": f"Lỗi tạo ảnh: {img_url}"})
            elif itype == "task_mode":
                current_task = item["task"]
                img_b64 = item.get("img_b64")
                mime = item.get("mime", "image/png")
                update_session_state(sid, phase="Quản lý chia nhỏ task...", think={"s": "manager", "a": "📋", "txt": "Quản lý đang mổ xẻ các giai đoạn..."})
                subtasks = split_into_subtasks(current_task)
                roadmap_str = "\n".join([f"• {st}" for st in subtasks])
                broadcast_to_session(sid, {
                    "s": "manager", "a": "📋", "n": "Quản lý", "b": "Lộ trình Task",
                    "txt": f"⚡ [TASK MODE] Đã chia thành {len(subtasks)} giai đoạn:\n{roadmap_str}"
                })
                for idx, stask in enumerate(subtasks):
                    broadcast_to_session(sid, {
                        "s": "sys", "a": "🚀", "n": "Hệ Thống", "b": f"Giai đoạn {idx+1}/{len(subtasks)}",
                        "txt": f"Bắt đầu: {stask}"
                    })
                    execute_engine_pipeline(sid, stask, img_b64, mime)
                    time.sleep(1)
                broadcast_to_session(sid, {
                    "s": "sys", "a": "🎉", "n": "Hệ Thống", "b": "Hoàn tất",
                    "txt": "Đã hoàn thành toàn bộ các giai đoạn của dự án trong phiên này!"
                })
            elif itype == "single_task":
                current_task = item["task"]
                img_b64 = item.get("img_b64")
                mime = item.get("mime", "image/png")
                
                update_session_state(sid, phase="Inquiry khảo sát...", think={"s": "inquiry", "a": "🔍", "txt": "Inquiry đang khảo sát đề bài..."})
                _, existing_code = get_current_codebase(sid)
                has_existing = bool(existing_code.strip())

                p_inq = (
                    "Bạn là Chuyên gia Phân tích Đề bài (Inquiry).\n"
                    f"Khảo sát: '{current_task}'.\n"
                    f"Trạng thái: {'ĐÃ CÓ MÃ NGUỒN CŨ' if has_existing else 'PHIÊN MỚI TINH'}.\n"
                    "Đánh giá xem đề bài có bị thiếu thông số hoặc mơ hồ nghiêm trọng không.\n"
                    "TRẢ VỀ JSON: {\"need_clarification\": true/false, \"question\": \"câu hỏi nếu true\", \"clarified_task\": \"đề bài rõ nếu false\"}"
                )
                try:
                    res_inq = call_gemini_smart(p_inq, img_b64, mime, max_tokens=512, timeout=15)
                    inq_data = parse_json_safe(res_inq)
                except Exception:
                    inq_data = {"need_clarification": False, "clarified_task": current_task}

                if inq_data.get("need_clarification"):
                    WAITING_CLARIFICATIONS[sid] = True
                    PENDING_ORIGIN_TASKS[sid] = current_task
                    q_text = inq_data.get("question", "Cần làm rõ thêm thông số.")
                    broadcast_to_session(sid, {"s": "inquiry", "a": "🔍", "n": "Inquiry", "b": "Khảo sát", "txt": q_text, "is_q": True})
                else:
                    task_ready = inq_data.get("clarified_task", current_task)
                    broadcast_to_session(sid, {"s": "inquiry", "a": "🔍", "n": "Inquiry", "b": "Khảo sát", "txt": f"Yêu cầu: {task_ready}"})
                    execute_engine_pipeline(sid, task_ready, img_b64, mime)

            elif itype == "clarified_pipeline":
                execute_engine_pipeline(sid, item["task"], item.get("img_b64"), item.get("mime", "image/png"))

        except Exception as e:
            traceback.print_exc()
            broadcast_to_session(sid, {"s": "sys", "a": "⚠️", "n": "Hệ Thống", "b": "Lỗi", "txt": f"Tiến trình bị gián đoạn: {str(e)}"})
        finally:
            update_session_state(sid, is_busy=False, task_title="", phase="", think={})
            TASK_QUEUE.task_done()

worker_thread = threading.Thread(target=background_worker, daemon=True)
worker_thread.start()

# API Quản lý Session
@app.route("/api/sessions", methods=["GET"])
def api_get_sessions():
    return jsonify(load_sessions_meta())

@app.route("/api/sessions/create", methods=["POST"])
def api_create_session():
    data = request.json or {}
    title = data.get("title", "").strip() or "Phiên làm việc mới"
    new_id = str(uuid.uuid4())[:8]
    meta = load_sessions_meta()
    meta.insert(0, {"id": new_id, "title": title, "created_at": time.time()})
    save_sessions_meta(meta)
    get_session_dir(new_id)
    return jsonify({"status": "ok", "id": new_id, "title": title})

@app.route("/api/sessions/<sid>/delete", methods=["POST"])
def api_delete_session(sid):
    meta = load_sessions_meta()
    meta = [s for s in meta if s["id"] != sid]
    if not meta:
        meta = [{"id": "default", "title": "Phiên làm việc chính", "created_at": time.time()}]
    save_sessions_meta(meta)
    sdir = get_session_dir(sid)
    for f in glob.glob(os.path.join(sdir, "*")):
        try: os.remove(f)
        except Exception: pass
    try: os.rmdir(sdir)
    except Exception: pass
    return jsonify({"status": "ok"})

@app.route("/preview")
def prev():
    sid = request.args.get("sid", "default")
    fpath = os.path.join(get_session_dir(sid), "index.html")
    return send_file(fpath) if os.path.exists(fpath) else ("<h3>Phiên này chưa có web index.html</h3>", 404)

@app.route("/download_zip")
def download_zip():
    sid = request.args.get("sid", "default")
    sdir = get_session_dir(sid)
    zip_name = f"project_{sid}.zip"
    with zipfile.ZipFile(zip_name, "w", zipfile.ZIP_DEFLATED) as z:
        for f in glob.glob(os.path.join(sdir, "*")):
            if os.path.isfile(f):
                z.write(f, os.path.basename(f))
    return send_file(zip_name, as_attachment=True)

@app.route("/workspace_file/<sid>/<path:filename>")
def serve_workspace_file(sid, filename):
    return send_from_directory(get_session_dir(sid), filename)

@app.route("/upload", methods=["POST"])
def upload_file_api():
    sid = request.args.get("sid", "default")
    if "file" not in request.files:
        return jsonify({"status": "error", "error": "Không có tệp"})
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"status": "error", "error": "Chưa chọn tệp"})

    fname = file.filename
    fpath = os.path.join(get_session_dir(sid), fname)
    file.save(fpath)

    is_img = fname.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
    text_content = ""
    img_b64 = None
    mime = "image/png"

    if is_img:
        if fname.lower().endswith((".jpg", ".jpeg")): mime = "image/jpeg"
        elif fname.lower().endswith(".webp"): mime = "image/webp"
        with open(fpath, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")
    else:
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                text_content = f.read()
        except Exception:
            text_content = ""

    LAST_UPLOADED_FILES[sid] = {
        "filename": fname,
        "is_img": is_img,
        "text_content": text_content,
        "img_b64": img_b64,
        "mime": mime
    }
    return jsonify({"status": "ok", "filename": fname, "is_img": is_img})

@app.route("/clear", methods=["POST"])
def clear_history():
    sid = request.args.get("sid", "default")
    save_session_history(sid, [])
    fpath = os.path.join(get_session_dir(sid), "history.json")
    if os.path.exists(fpath):
        try: os.remove(fpath)
        except Exception: pass
    return jsonify({"status": "ok"})

@app.route("/reset_project", methods=["POST"])
def reset_project():
    sid = request.args.get("sid", "default")
    sdir = get_session_dir(sid)
    for f in ["index.html", "script.py"]:
        p = os.path.join(sdir, f)
        if os.path.exists(p): os.remove(p)
    return jsonify({"status": "ok"})

HTML = r'''<!DOCTYPE html>
<html lang="vi" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,interactive-widget=resizes-content">
<title>5-AI Multi-Session Studio</title>
<style>
:root[data-theme="dark"]{
  --bg:#090d16;--surface:#111827;--surface-card:#161f32;--border:#243048;--border-focus:#38bdf8;
  --txt:#f1f5f9;--txt-muted:#94a3b8;--pri:#38bdf8;--pri-glow:rgba(56,189,248,0.3);
  --code-bg:#050811;--user-bg:#1e293b;--sidebar-bg:#0d121f;--shadow:0 8px 24px rgba(0,0,0,0.5);
}
:root[data-theme="light"]{
  --bg:#f8fafc;--surface:#ffffff;--surface-card:#f1f5f9;--border:#e2e8f0;--border-focus:#0284c7;
  --txt:#0f172a;--txt-muted:#64748b;--pri:#0284c7;--pri-glow:rgba(2,132,199,0.25);
  --code-bg:#0f172a;--user-bg:#e2e8f0;--sidebar-bg:#f1f5f9;--shadow:0 6px 20px rgba(0,0,0,0.06);
}
*{box-sizing:border-box;margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;-webkit-tap-highlight-color:transparent}
body{background:var(--bg);color:var(--txt);height:100dvh;display:flex;flex-direction:row;overflow:hidden;transition:background .25s ease,color .25s ease}

#sidebar{width:260px;background:var(--sidebar-bg);border-right:1px solid var(--border);display:flex;flex-direction:column;z-index:40;transition:transform .3s ease}
@media (max-width:768px){
  #sidebar{position:fixed;top:0;bottom:0;left:0;transform:translateX(-100%);box-shadow:var(--shadow)}
  #sidebar.open{transform:translateX(0)}
  #sidebarOverlay{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:35;display:none}
  #sidebarOverlay.open{display:block}
}
.sidebar-hdr{padding:12px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border)}
.btn-new-chat{width:100%;height:38px;padding:0 12px;border-radius:10px;background:var(--surface);border:1px solid var(--border);color:var(--txt);font-weight:700;font-size:.82rem;display:flex;align-items:center;gap:8px;cursor:pointer;transition:all .15s}
.btn-new-chat:hover{border-color:var(--pri);color:var(--pri)}
.session-list{flex:1;overflow-y:auto;padding:8px;display:flex;flex-direction:column;gap:4px}
.session-item{display:flex;align-items:center;justify-content:space-between;padding:8px 10px;border-radius:8px;font-size:.8rem;color:var(--txt-muted);cursor:pointer;transition:background .15s}
.session-item:hover{background:var(--surface);color:var(--txt)}
.session-item.active{background:var(--surface);color:var(--pri);font-weight:700;border:1px solid var(--border)}
.session-title{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-right:6px}
.btn-del-session{background:transparent;border:none;color:#ef4444;cursor:pointer;padding:2px 4px;opacity:.6}
.btn-del-session:hover{opacity:1}

#mainView{flex:1;display:flex;flex-direction:column;height:100dvh;overflow:hidden;position:relative}

header{padding:8px 12px;background:var(--surface);border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px;z-index:20;box-shadow:var(--shadow)}
.brand-group{display:flex;align-items:center;gap:8px}
.btn-menu{width:32px;height:32px;border-radius:8px;border:1px solid var(--border);background:var(--surface-card);color:var(--txt);cursor:pointer;display:inline-flex;align-items:center;justify-content:center}
.status-pill{display:inline-flex;align-items:center;gap:6px;padding:4px 9px;border-radius:20px;background:var(--surface-card);border:1px solid var(--border);font-size:.75rem;font-weight:700}
.dot-st{width:8px;height:8px;border-radius:50%;background:#22c55e;transition:all .3s}
.dot-st.busy{background:#f59e0b;box-shadow:0 0 10px #f59e0b;animation:pulseDot 1.2s infinite ease-in-out}
@keyframes pulseDot{0%,100%{transform:scale(1);opacity:1}50%{transform:scale(1.35);opacity:.6}}

.btn-cluster{display:flex;align-items:center;gap:5px;flex-wrap:wrap}
.ui-btn{height:28px;padding:0 9px;border-radius:7px;border:1px solid var(--border);background:var(--surface-card);color:var(--txt);font-size:.72rem;font-weight:700;display:inline-flex;align-items:center;gap:5px;cursor:pointer;transition:all .2s ease;text-decoration:none}
.ui-btn:active{transform:scale(.95)}
.ui-btn.task-on{background:#059669;border-color:#10b981;color:#fff;box-shadow:0 0 10px rgba(16,185,129,.4)}
.ui-btn.btn-theme{padding:0 7px}
.ui-btn.btn-reset{color:#f59e0b;border-color:rgba(245,158,11,.3)}
.ui-btn.btn-clear{color:#ef4444;border-color:rgba(239,68,68,.3)}

#box{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:12px;scroll-behavior:smooth}
.row{display:flex;gap:10px;align-items:flex-start;animation:fadeSlideIn .25s ease-out forwards;opacity:0;transform:translateY(8px)}
@keyframes fadeSlideIn{to{opacity:1;transform:translateY(0)}}

.avt-wrap{width:34px;height:34px;border-radius:10px;display:flex;align-items:center;justify-content:center;flex-shrink:0;box-shadow:0 3px 8px rgba(0,0,0,.25)}
.user .avt-wrap{background:#475569;color:#fff}
.inquiry .avt-wrap{background:#0d9488;color:#fff}
.manager .avt-wrap{background:#ea580c;color:#fff}
.dev .avt-wrap{background:#2563eb;color:#fff}
.tester .avt-wrap{background:#059669;color:#fff}
.checker .avt-wrap{background:#7c3aed;color:#fff}
.sys .avt-wrap{background:#dc2626;color:#fff}
.artist .avt-wrap{background:#ec4899;color:#fff}

.msg{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:10px 14px;flex:1;font-size:.88rem;line-height:1.5;box-shadow:var(--shadow);overflow-x:hidden}
.user .msg{background:var(--user-bg);border-color:var(--border)}
.header-meta{display:flex;align-items:center;gap:6px;margin-bottom:4px}
.badge-role{font-size:.65rem;font-weight:800;padding:2px 7px;border-radius:6px;background:var(--surface-card);border:1px solid var(--border);color:var(--txt-muted);letter-spacing:.5px}

pre{background:var(--code-bg);color:#38bdf8;padding:10px 12px;border-radius:8px;overflow-x:auto;font-size:.8rem;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;margin-top:8px;border:1px solid var(--border)}
.action-bar{margin-top:8px;display:flex;gap:6px;flex-wrap:wrap}
.action-btn{height:28px;padding:0 10px;border-radius:6px;font-size:.72rem;font-weight:700;display:inline-flex;align-items:center;gap:5px;cursor:pointer;border:none;color:#fff;text-decoration:none;transition:transform .15s}
.action-btn:active{transform:scale(.95)}
.btn-dl{background:#0d9488}
.btn-preview{background:#2563eb}
.btn-copy{background:#475569}

.verdict{display:inline-flex;align-items:center;gap:5px;padding:3px 8px;border-radius:6px;font-size:.72rem;font-weight:800;margin-top:6px}
.verdict.APPROVED{background:rgba(16,185,129,.15);color:#10b981;border:1px solid rgba(16,185,129,.3)}
.verdict.REVISE{background:rgba(239,68,68,.15);color:#ef4444;border:1px solid rgba(239,68,68,.3)}
.question-notice{margin-top:6px;padding:8px 12px;background:rgba(245,158,11,.12);border-left:3px solid #f59e0b;color:#f59e0b;border-radius:4px;font-size:.82rem;font-weight:700}
.preview-img{max-width:100%;max-height:280px;border-radius:10px;margin-top:8px;border:1px solid var(--border);display:block}

.think-bar{display:flex;align-items:center;gap:8px;font-size:.78rem;color:var(--txt-muted);font-weight:600}
.wave-dots{display:flex;gap:4px}
.wave-dot{width:6px;height:6px;border-radius:50%;background:var(--pri);animation:wave 1.2s infinite ease-in-out}
.wave-dot:nth-child(2){animation-delay:.15s}
.wave-dot:nth-child(3){animation-delay:.3s}
@keyframes wave{0%,80%,100%{transform:scale(0.6);opacity:0.3}40%{transform:scale(1.15);opacity:1}}

footer{padding:8px 12px calc(10px + env(safe-area-inset-bottom,8px));background:var(--surface);border-top:1px solid var(--border);display:flex;gap:6px;align-items:center;z-index:20}
.btn-icon{width:38px;height:38px;border-radius:50%;border:1px solid var(--border);background:var(--surface-card);color:var(--txt);cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .2s;flex-shrink:0}
.btn-icon:active{transform:scale(.92)}
input[type="text"]{flex:1;height:40px;padding:0 14px;border:1px solid var(--border);border-radius:20px;outline:none;background:var(--surface-card);color:var(--txt);font-size:.88rem;transition:border-color .2s,box-shadow .2s}
input[type="text"]:focus{border-color:var(--border-focus);box-shadow:0 0 0 3px var(--pri-glow)}
.btn-send{height:40px;padding:0 16px;background:var(--pri);color:#090d16;border:none;border-radius:20px;font-weight:800;font-size:.84rem;cursor:pointer;display:inline-flex;align-items:center;gap:6px;transition:all .15s}
.btn-send:active{transform:scale(.95)}
</style>
</head>
<body>

<div id="sidebarOverlay" onclick="toggleSidebar()"></div>

<aside id="sidebar">
  <div class="sidebar-hdr">
    <button class="btn-new-chat" onclick="createNewSession()">
      <span id="iconPlus"></span> + Đoạn chat mới
    </button>
  </div>
  <div class="session-list" id="sessionList"></div>
</aside>

<div id="mainView">
  <header>
    <div class="brand-group">
      <button class="btn-menu" onclick="toggleSidebar()" id="btnMenu"></button>
      <div class="status-pill">
        <span class="dot-st" id="netDot"></span>
        <span id="netTxt">5-AI Sẵn sàng</span>
      </div>
    </div>
    <div class="btn-cluster">
      <button class="ui-btn btn-theme" id="btnTheme" onclick="toggleTheme()" title="Đổi giao diện"></button>
      <button class="ui-btn" id="btnTask" onclick="toggleTaskMode()"></button>
      <a class="ui-btn" id="btnZip" href="#"></a>
      <button class="ui-btn btn-reset" onclick="resetProject()">Mới</button>
      <button class="ui-btn btn-clear" onclick="clearHistory()">Xóa</button>
    </div>
  </header>

  <div id="box"></div>

  <footer>
    <input type="file" id="fileInp" style="display:none" onchange="uploadSelectedFile()">
    <button class="btn-icon" onclick="document.getElementById('fileInp').click()" title="Đính kèm file hoặc ảnh" id="btnClip"></button>
    <input type="text" id="in" placeholder="Nhập yêu cầu phát triển cho phiên này..." autocomplete="off">
    <button class="btn-send" onclick="send()" id="btnSend"><span>Gửi</span></button>
  </footer>
</div>

<script>
const ICONS = {
  menu: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="18" x2="21" y2="18"/></svg>',
  plus: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>',
  trash: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
  user: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>',
  inquiry: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>',
  manager: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>',
  dev: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>',
  tester: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10 2v7.31L4.2 18A2 2 0 0 0 6 21h12a2 2 0 0 0 1.8-3L14 9.31V2"/></svg>',
  checker: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
  artist: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="13.5" cy="6.5" r=".5" fill="currentColor"/><circle cx="17.5" cy="10.5" r=".5" fill="currentColor"/><circle cx="8.5" cy="7.5" r=".5" fill="currentColor"/><circle cx="6.5" cy="12.5" r=".5" fill="currentColor"/><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.9 0 1.7-.8 1.7-1.7 0-.4-.2-.8-.4-1.1-.3-.3-.4-.7-.4-1.2 0-.9.8-1.7 1.7-1.7h2c3.3 0 6-2.7 6-6 0-5.5-4.5-10-10-10z"/></svg>',
  sys: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  bolt: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>',
  zip: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>',
  sun: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>',
  moon: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>',
  clip: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>',
  send: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>',
  download: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>',
  eye: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>',
  copy: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>'
};

document.getElementById('btnMenu').innerHTML = ICONS.menu;
document.getElementById('iconPlus').innerHTML = ICONS.plus;
document.getElementById('btnClip').innerHTML = ICONS.clip;
document.getElementById('btnSend').insertAdjacentHTML('afterbegin', ICONS.send);

let currentSid = localStorage.getItem('hub_active_sid') || 'default';
let ws = null, thinkRow = null, taskMode = false, wsTimer = null;
const box = document.getElementById('box'), inp = document.getElementById('in');
const netDot = document.getElementById('netDot'), netTxt = document.getElementById('netTxt');

function initTheme(){
  const saved = localStorage.getItem('hub_theme') || 'dark';
  document.documentElement.setAttribute('data-theme', saved);
  document.getElementById('btnTheme').innerHTML = saved === 'dark' ? ICONS.sun : ICONS.moon;
}
function toggleTheme(){
  const curr = document.documentElement.getAttribute('data-theme');
  const next = curr === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('hub_theme', next);
  document.getElementById('btnTheme').innerHTML = next === 'dark' ? ICONS.sun : ICONS.moon;
}
initTheme();

function toggleSidebar(){
  document.getElementById('sidebar').classList.toggle('open');
  document.getElementById('sidebarOverlay').classList.toggle('open');
}

function updateZipBtnLink(){
  document.getElementById('btnZip').href = `/download_zip?sid=${currentSid}`;
  document.getElementById('btnZip').innerHTML = `${ICONS.zip} Zip`;
}

function toggleTaskMode(){
  taskMode = !taskMode;
  const b = document.getElementById('btnTask');
  b.classList.toggle('task-on', taskMode);
  b.innerHTML = `${ICONS.bolt} Task: ${taskMode ? 'BẬT' : 'TẮT'}`;
}
toggleTaskMode(); toggleTaskMode();

function esc(s){return s?s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'):'';}

function downloadFile(content, filename){
  const blob = new Blob([content], {type:'text/plain;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}

function copyCode(content, btn){
  navigator.clipboard.writeText(content).then(()=>{
    const old = btn.innerHTML;
    btn.innerHTML = '✔ Đã chép';
    setTimeout(()=>{ btn.innerHTML = old; }, 1500);
  });
}

function renderMessage(d){
  const r = document.createElement('div');
  r.className = `row ${d.s}`;

  const avtWrap = document.createElement('div');
  avtWrap.className = 'avt-wrap';
  avtWrap.innerHTML = ICONS[d.s] || ICONS.dev;

  const msgDiv = document.createElement('div');
  msgDiv.className = 'msg';

  let badgeHtml = d.b ? `<span class="badge-role">${esc(d.b)}</span>` : '';
  msgDiv.innerHTML = `<div class="header-meta"><b>${d.n}</b>${badgeHtml}</div><div style="white-space:pre-wrap">${esc(d.txt)}</div>`;

  if(d.img_url){
    const img = document.createElement('img');
    img.src = d.img_url; img.className = 'preview-img';
    msgDiv.appendChild(img);
    const dlLink = document.createElement('a');
    dlLink.href = d.img_url; dlLink.download = 'image.png'; dlLink.className = 'action-btn btn-dl';
    dlLink.style.marginTop = '8px'; dlLink.innerHTML = `${ICONS.download} Tải ảnh`;
    msgDiv.appendChild(dlLink);
  }

  if(d.is_q){
    const qBox = document.createElement('div');
    qBox.className = 'question-notice';
    qBox.textContent = '👉 Đang đợi bạn làm rõ: Nhập câu trả lời để AI tiếp tục quy trình.';
    msgDiv.appendChild(qBox);
    inp.placeholder = 'Trả lời câu hỏi của Inquiry...';
    inp.focus();
  }

  if(d.code){
    const pre = document.createElement('pre');
    pre.innerHTML = `<code>${esc(d.code)}</code>`;
    msgDiv.appendChild(pre);

    const bar = document.createElement('div');
    bar.className = 'action-bar';
    const isHtml = d.code.includes('<html') || d.code.includes('<!DOCTYPE') || d.code.includes('<script');
    const fName = isHtml ? 'index.html' : 'script.py';

    const cpBtn = document.createElement('button');
    cpBtn.className = 'action-btn btn-copy';
    cpBtn.innerHTML = `${ICONS.copy} Chép`;
    cpBtn.onclick = () => copyCode(d.code, cpBtn);
    bar.appendChild(cpBtn);

    const dlBtn = document.createElement('button');
    dlBtn.className = 'action-btn btn-dl';
    dlBtn.innerHTML = `${ICONS.download} ${fName}`;
    dlBtn.onclick = () => downloadFile(d.code, fName);
    bar.appendChild(dlBtn);

    if(isHtml){
      const prevLink = document.createElement('a');
      prevLink.className = 'action-btn btn-preview';
      prevLink.href = `/preview?sid=${currentSid}`; prevLink.target = '_blank';
      prevLink.innerHTML = `${ICONS.eye} Xem Web`;
      bar.appendChild(prevLink);
    }
    msgDiv.appendChild(bar);
  }

  if(d.log){
    const preLog = document.createElement('pre');
    preLog.style.background = '#020617'; preLog.style.color = '#38bdf8';
    preLog.innerHTML = `<code>${esc(d.log)}</code>`;
    msgDiv.appendChild(preLog);
  }

  if(d.st){
    const v = document.createElement('div');
    v.className = `verdict ${d.st}`;
    v.innerHTML = d.st === 'APPROVED' ? '✔ CHUẨN DUYỆT (HOÀN THÀNH)' : '↺ CẦN SỬA ĐỔI';
    msgDiv.appendChild(v);
  }

  r.appendChild(avtWrap);
  r.appendChild(msgDiv);
  box.appendChild(r);
}

function applyState(st){
  if(!st) return;
  if(st.is_busy){
    netDot.className = 'dot-st busy';
    netTxt.textContent = st.phase || 'Đang xử lý ngầm...';
  } else {
    netDot.className = 'dot-st';
    netTxt.textContent = '5-AI Sẵn sàng';
  }

  if(thinkRow){ thinkRow.remove(); thinkRow = null; }
  if(st.think){
    thinkRow = document.createElement('div');
    thinkRow.className = `row ${st.think.s}`;

    const avtWrap = document.createElement('div');
    avtWrap.className = 'avt-wrap';
    avtWrap.innerHTML = ICONS[st.think.s] || ICONS.dev;

    const msg = document.createElement('div');
    msg.className = 'msg';
    msg.innerHTML = `
      <div class="think-bar">
        <span>${esc(st.think.txt)}</span>
        <div class="wave-dots"><div class="wave-dot"></div><div class="wave-dot"></div><div class="wave-dot"></div></div>
      </div>
    `;
    thinkRow.appendChild(avtWrap);
    thinkRow.appendChild(msg);
    box.appendChild(thinkRow);
    box.scrollTop = box.scrollHeight;
  }
}

function loadSessionsList(){
  fetch('/api/sessions').then(r => r.json()).then(sessions => {
    const listDiv = document.getElementById('sessionList');
    listDiv.innerHTML = '';
    sessions.forEach(s => {
      const item = document.createElement('div');
      item.className = `session-item ${s.id === currentSid ? 'active' : ''}`;
      item.innerHTML = `
        <span class="session-title" onclick="switchSession('${s.id}')">${esc(s.title)}</span>
        <button class="btn-del-session" onclick="deleteSession('${s.id}')" title="Xóa phiên">${ICONS.trash}</button>
      `;
      listDiv.appendChild(item);
    });
  });
}

function switchSession(sid){
  if(sid === currentSid) return;box.innerHTML='';if(thinkRow){thinkRow.remove();thinkRow=null;}
  currentSid = sid;
  localStorage.setItem('hub_active_sid', currentSid);
  updateZipBtnLink();
  loadSessionsList();
  if(window.innerWidth <= 768) toggleSidebar();
  connectWS();
}

function createNewSession(){
  const t = prompt("Đặt tên cho Phiên làm việc mới:", "Dự án " + new Date().toLocaleTimeString());
  if(!t) return;
  fetch('/api/sessions/create', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({title: t})
  }).then(r => r.json()).then(res => {
    if(res.status === 'ok'){
      currentSid = res.id;
      localStorage.setItem('hub_active_sid', currentSid);
      updateZipBtnLink();
      loadSessionsList();
      if(window.innerWidth <= 768) toggleSidebar();
      connectWS();
    }
  });
}

function deleteSession(sid){
  if(!confirm("Xóa toàn bộ dữ liệu và mã nguồn của phiên làm việc này?")) return;
  fetch(`/api/sessions/${sid}/delete`, {method: 'POST'}).then(r => r.json()).then(() => {
    if(currentSid === sid) {
      currentSid = 'default';
      localStorage.setItem('hub_active_sid', currentSid);
    }
    updateZipBtnLink();
    loadSessionsList();
    connectWS();
  });
}

function connectWS(){
  clearTimeout(wsTimer);if(ws){ws.onclose=null;ws.onerror=null;
    try { ws.close(); } catch(e){}
  }
  updateZipBtnLink();
  ws = new WebSocket(`ws://${location.host}/ws?sid=${currentSid}`);
  ws.onopen = () => {
    netDot.className = 'dot-st';
    clearTimeout(wsTimer);netTxt.textContent = 'Đã kết nối';
  };
  ws.onclose = () => {
    netDot.className = 'dot-st';
    netDot.style.background = '#ef4444';
    netTxt.textContent = 'Mất kết nối, thử lại...';
    wsTimer=setTimeout(connectWS, 1500);
  };
  ws.onerror = () => { ws.close(); };
  ws.onmessage = e => {
    const d = JSON.parse(e.data);
    if(d.t === 'init_sync'){
      box.innerHTML = '';
      d.history.forEach(renderMessage);
      applyState(d.state);
      box.scrollTop = box.scrollHeight;
      return;
    }
    if(d.t === 'state_sync'){
      applyState(d.state);
      return;
    }
    if(thinkRow){ thinkRow.remove(); thinkRow = null; }
    renderMessage(d);
    box.scrollTop = box.scrollHeight;
  };
}

loadSessionsList();
connectWS();

function send(){
  const v = inp.value.trim(); if(!v) return;
  if(v.toLowerCase() === 'clear' || v.toLowerCase() === '/clear' || v.toLowerCase() === 'cls'){
    inp.value = '';
    fetch(`/clear?sid=${currentSid}`, {method:'POST'}).then(()=>{ box.innerHTML = ''; });
    return;
  }
  if(!ws || ws.readyState !== WebSocket.OPEN){
    alert("Đang kết nối lại máy chủ, thử lại sau 1 giây!");
    return;
  }
  ws.send(JSON.stringify({text: v, task_mode: taskMode, sid: currentSid}));
  inp.value = '';
  inp.placeholder = 'Nhập yêu cầu phát triển cho phiên này...';
}

function uploadSelectedFile(){
  const fInp = document.getElementById('fileInp');
  if(!fInp.files || fInp.files.length === 0) return;
  const file = fInp.files[0];
  const fd = new FormData();
  fd.append('file', file);
  fetch(`/upload?sid=${currentSid}`, {method:'POST', body:fd})
    .then(r => r.json())
    .then(res => {
      if(res.status === 'ok'){
        inp.value = `[ĐÃ ĐÍNH KÈM: ${res.filename}] ` + inp.value;
        inp.focus();
      }
      fInp.value = '';
    });
}

function resetProject(){
  if(confirm("Dọn sạch mã nguồn cũ trong phiên này để code lại từ đầu?")){
    fetch(`/reset_project?sid=${currentSid}`, {method:'POST'}).then(() => {
      alert("Đã làm mới mã nguồn của phiên hiện tại!");
      location.reload();
    });
  }
}

function clearHistory(){
  if(confirm("Xoá toàn bộ lịch sử hội thoại của phiên này?")){
    fetch(`/clear?sid=${currentSid}`, {method:'POST'}).then(() => { box.innerHTML = ''; });
  }
}

inp.onkeydown = e => { if(e.key === 'Enter') send(); };
</script>
</body>
</html>'''

@app.route("/")
def home():
    return Response(HTML, mimetype="text/html")

@sock.route("/ws")
def ws_loop(ws):
    sid = request.args.get("sid", "default")
    ACTIVE_CLIENTS[ws] = sid

    hist = get_session_history(sid)
    st = SESSION_STATES.get(sid, {"is_busy": False, "task_title": "", "phase": "", "think": None})
    try:
        ws.send(json.dumps({"t": "init_sync", "history": hist, "state": st}))
    except Exception:
        pass

    try:
        while True:
            raw_msg = ws.receive()
            if not raw_msg: break

            task_mode = False
            user_text = raw_msg
            current_sid = sid
            try:
                p_obj = json.loads(raw_msg)
                if isinstance(p_obj, dict):
                    user_text = p_obj.get("text", user_text)
                    task_mode = p_obj.get("task_mode", False)
                    current_sid = p_obj.get("sid", sid)
            except Exception:
                pass

            attached_img_b64 = None
            attached_mime = "image/png"
            attached_note = ""

            last_file = LAST_UPLOADED_FILES.pop(current_sid, None)
            if last_file:
                if last_file["is_img"]:
                    attached_img_b64 = last_file["img_b64"]
                    attached_mime = last_file["mime"]
                    attached_note = f"\n[FILE HÌNH ẢNH: {last_file['filename']}]"
                else:
                    snippet = last_file['text_content'][:4000]
                    attached_note = f"\n[TỆP ĐÍNH KÈM {last_file['filename']}]:\n```{snippet}```"

            if WAITING_CLARIFICATIONS.get(current_sid):
                broadcast_to_session(current_sid, {"s": "user", "a": "👤", "n": "Bạn", "txt": f"Trả lời: {user_text}"})
                combined_task = f"{PENDING_ORIGIN_TASKS.get(current_sid, '')} (Làm rõ: {user_text})" + attached_note
                WAITING_CLARIFICATIONS[current_sid] = False
                broadcast_to_session(current_sid, {"s": "inquiry", "a": "🔍", "n": "Inquiry", "b": "Khảo sát", "txt": "Đã ghi nhận thông tin làm rõ. Tiếp tục luồng xử lý!"})
                TASK_QUEUE.put({
                    "session_id": current_sid,
                    "type": "clarified_pipeline",
                    "task": combined_task,
                    "img_b64": attached_img_b64,
                    "mime": attached_mime
                })
            else:
                full_task = user_text + attached_note
                broadcast_to_session(current_sid, {"s": "user", "a": "👤", "n": "Bạn", "txt": user_text})

                meta = load_sessions_meta()
                for s in meta:
                    if s["id"] == current_sid and s["title"] in ("Phiên làm việc chính", "Phiên làm việc mới"):
                        s["title"] = user_text[:28] + ("..." if len(user_text) > 28 else "")
                        save_sessions_meta(meta)
                        break

                img_triggers = ["tạo ảnh", "vẽ ảnh", "generate image", "tạo hình", "vẽ cho tôi", "draw image", "render ảnh"]
                if any(trig in user_text.lower() for trig in img_triggers):
                    TASK_QUEUE.put({"session_id": current_sid, "type": "image", "text": user_text})
                elif task_mode:
                    TASK_QUEUE.put({
                        "session_id": current_sid,
                        "type": "task_mode",
                        "task": full_task,
                        "img_b64": attached_img_b64,
                        "mime": attached_mime
                    })
                else:
                    TASK_QUEUE.put({
                        "session_id": current_sid,
                        "type": "single_task",
                        "task": full_task,
                        "img_b64": attached_img_b64,
                        "mime": attached_mime
                    })
    finally:
        ACTIVE_CLIENTS.pop(ws, None)

if __name__ == "__main__":
    os.system("clear")
    logo = """\033[1;36m
███████╗███████╗███╗   ██╗ ██████╗ ██╗███╗   ██╗███████╗
██╔════╝██╔════╝████╗  ██║██╔════╝ ██║████╗  ██║██╔════╝
█████╗  █████╗  ██╔██╗ ██║██║  ███╗██║██╔██╗ ██║█████╗  
██╔══╝  ██╔══╝  ██║╚██╗██║██║   ██║██║██║╚██╗██║██╔══╝  
██║██╗  ███████╗██║ ╚████║╚██████╔╝██║██║ ╚████║███████╗
╚═╝╚═╝  ╚══════╝╚═╝  ╚═══╝ ╚═════╝ ╚═╝╚═╝  ╚═══╝╚══════╝\033[0m"""
    print(logo)
    print("\033[1;32m[+] 5-AI Engine Multi-Session Studio đang hoạt động...\033[0m")
    print("\033[1;34m[+] Web UI: http://127.0.0.1:8000\033[0m\n")
    app.run(host="0.0.0.0", port=8000)
