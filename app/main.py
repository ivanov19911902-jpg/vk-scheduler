import os
import asyncio
import httpx
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Form, Depends, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session
from passlib.context import CryptContext

from app.config import settings
from app.database import get_db, init_db, SessionLocal
from app.models import User, Community, Batch, BatchStatus
from app.services.crypto import encrypt, decrypt

os.makedirs("data", exist_ok=True)
os.makedirs(settings.upload_dir, exist_ok=True)

pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db = SessionLocal()
    try:
        if not db.query(User).first() and settings.admin_password:
            db.add(User(username=settings.admin_username, password_hash=pwd.hash(settings.admin_password)))
            db.commit()
            print(f"[init] Создан админ: {settings.admin_username}")
    finally:
        db.close()
    yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax", https_only=False)
templates = Jinja2Templates(directory="app/templates")

def current_user(request: Request, db: Session) -> User | None:
    uid = request.session.get("user_id")
    return db.get(User, uid) if uid else None

async def post_to_vk(group_id: int, token: str, text: str, photo_path: str):
    async with httpx.AsyncClient() as client:
        upload_server = await client.get(
            "https://api.vk.com/method/photos.getWallUploadServer",
            params={"group_id": group_id, "access_token": token, "v": "5.199"}
        )
        upload_json = upload_server.json()
        if "error" in upload_json:
            raise Exception(f"Ошибка загрузки сервера: {upload_json['error'].get('error_msg')}")
        upload_url = upload_json["response"]["upload_url"]
        
        with open(photo_path, "rb") as f:
            files = {"photo": f}
            upload_resp = await client.post(upload_url, files=files)
            upload_data = upload_resp.json()
            
        save_resp = await client.get(
            "https://api.vk.com/method/photos.saveWallPhoto",
            params={
                "group_id": group_id, "photo": upload_data["photo"], 
                "server": upload_data["server"], "hash": upload_data["hash"],
                "access_token": token, "v": "5.199"
            }
        )
        save_json = save_resp.json()
        if "error" in save_json:
             raise Exception(f"Ошибка сохранения фото: {save_json['error'].get('error_msg')}")
        photo_data = save_json["response"][0]
        attachment = f"photo{photo_data['owner_id']}_{photo_data['id']}"
        
        post_resp = await client.get(
            "https://api.vk.com/method/wall.post",
            params={
                "owner_id": f"-{group_id}", "message": text, "attachments": attachment,
                "access_token": token, "v": "5.199"
            }
        )
        post_json = post_resp.json()
        if "error" in post_json:
            raise Exception(f"Ошибка публикации: {post_json['error'].get('error_msg')}")
        return post_json["response"]["post_id"]


@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == username).first()
    if not user or not pwd.verify(password, user.password_hash):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Неверный логин или пароль"}, status_code=401)
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)

@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

@app.get("/", response_class=HTMLResponse)
async def send_page(request: Request, msg: str = None, error: str = None, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user: return RedirectResponse("/login", status_code=303)
    communities = db.query(Community).filter(Community.is_active == True).all()
    return templates.TemplateResponse("send.html", {"request": request, "user": user, "communities": communities, "msg": msg, "error": error})

@app.post("/send")
async def send_submit(
    request: Request,
    community_id: int = Form(...),
    texts: str = Form(...),
    photos: list[UploadFile] = File(...),
    db: Session = Depends(get_db)
):
    user = current_user(request, db)
    if not user: return RedirectResponse("/login", status_code=303)
    
    community = db.get(Community, community_id)
    token = decrypt(community.access_token_encrypted)
    
    text_list = [t.strip() for t in texts.split("---") if t.strip()]
    if len(photos) > len(text_list):
        return RedirectResponse("/?error=Ошибка:+текстов+меньше,+чем+фото", status_code=303)

    success_count = 0
    error_msgs = []

    for i in range(len(photos)):
        photo = photos[i]
        text = text_list[i]
        
        temp_path = f"{settings.upload_dir}/temp_{photo.filename}"
        with open(temp_path, "wb") as buffer:
            buffer.write(await photo.read())
            
        try:
            await post_to_vk(community.vk_group_id, token, text, temp_path)
            success_count += 1
        except Exception as e:
            error_msgs.append(f"Пост {i+1}: {str(e)}")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        
        await asyncio.sleep(0.5)

    if error_msgs:
        err_str = " | ".join(error_msgs[:2])
        return RedirectResponse(f"/?error=Отправлено:{success_count}.+Ошибки:+" + err_str, status_code=303)
    
    return RedirectResponse(f"/?msg=Успешно+опубликовано+постов:+{success_count}", status_code=303)

@app.get("/communities", response_class=HTMLResponse)
async def communities_page(request: Request, msg: str = None, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user: return RedirectResponse("/login", status_code=303)
    communities = db.query(Community).order_by(Community.created_at.desc()).all()
    return templates.TemplateResponse("communities.html", {"request": request, "user": user, "communities": communities, "msg": msg})

@app.post("/communities")
async def communities_add(request: Request, name: str = Form(...), vk_group_id: int = Form(...), access_token: str = Form(...), db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user: return RedirectResponse("/login", status_code=303)
    c = Community(name=name, vk_group_id=vk_group_id, access_token_encrypted=encrypt(access_token), token_valid=False)
    db.add(c)
    db.commit()
    return RedirectResponse("/communities?msg=Сообщество+успешно+сохранено!", status_code=303)
