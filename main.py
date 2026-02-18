import os
import re
import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from supabase import create_client

app = FastAPI()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

MEAL_TYPES = {"DESAYUNO", "ALMUERZO", "CENA", "SNACK"}

def tg_send(chat_id: int, text: str):
    if not TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text})

def parse_food_grams(text: str):
    t = text.strip()
    m = re.search(r"(.+?)\s+(\d+(?:[.,]\d+)?)\s*g?$", t, re.IGNORECASE)
    if not m:
        return None, None
    food = m.group(1).strip()
    grams = float(m.group(2).replace(",", "."))
    return food, grams

def get_or_create_user(telegram_id: int, display_name: str | None):
    res = sb.table("users").select("id,is_active").eq("telegram_id", telegram_id).limit(1).execute()
    if res.data:
        return res.data[0]["id"], bool(res.data[0]["is_active"])

    ins = sb.table("users").insert({
        "telegram_id": telegram_id,
        "display_name": display_name,
        "is_active": False
    }).execute()
    user_id = ins.data[0]["id"]

    sb.table("user_state").insert({
        "user_id": user_id,
        "state": "INACTIVE",
        "step": "NEED_CODE",
        "temp": {}
    }).execute()
    return user_id, False

def get_state(user_id: int):
    res = sb.table("user_state").select("step,temp").eq("user_id", user_id).limit(1).execute()
    if not res.data:
        return "NEED_CODE", {}
    return res.data[0].get("step") or "NEED_CODE", res.data[0].get("temp") or {}

def set_state(user_id: int, step: str, temp: dict):
    sb.table("user_state").update({"step": step, "temp": temp}).eq("user_id", user_id).execute()

@app.get("/")
def home():
    return {"status": "Bot running"}

@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    payload = await req.json()
    msg = payload.get("message") or payload.get("edited_message")
    if not msg:
        return {"ok": True}

    chat_id = msg["chat"]["id"]
    frm = msg.get("from", {})
    telegram_id = frm.get("id")
    text = (msg.get("text") or "").strip()

    if not telegram_id or not text:
        return {"ok": True}

    display_name = " ".join([x for x in [frm.get("first_name"), frm.get("last_name")] if x]).strip() or None
    user_id, is_active = get_or_create_user(telegram_id, display_name)

    # /start
    if text.lower() == "/start":
        step, _ = get_state(user_id)
        if not is_active:
            tg_send(chat_id, "Hola 👋 Para activar tu acceso, envíame tu CÓDIGO (ej: MVP-1001).")
        else:
            tg_send(chat_id, "Listo ✅ Ya estás activo. Escribe ALMUERZO/DESAYUNO/CENA/SNACK o 'pollo cocido 180g'.")
        return {"ok": True}

    # Si no está activo: pedir código y activar
    if not is_active:
        # Llamamos a tu función de Supabase activate_with_code
        try:
            rpc = sb.rpc("activate_with_code", {
                "p_telegram_id": telegram_id,
                "p_display_name": display_name,
                "p_code": text
            }).execute()
            row = rpc.data[0]
            if row["success"]:
                tg_send(chat_id, row["message"])
                # queda en step SEX
            else:
                tg_send(chat_id, row["message"] + " Intenta de nuevo.")
        except Exception:
            tg_send(chat_id, "No pude validar el código. Intenta nuevamente.")
        return {"ok": True}

    # Si está activo pero aún no READY: onboarding por pasos
    step, temp = get_state(user_id)
    upper = text.upper().strip()

    if step == "SEX":
        if upper not in {"H", "M"}:
            tg_send(chat_id, "Dime tu sexo: H (hombre) o M (mujer).")
            return {"ok": True}
        temp["sex"] = upper
        set_state(user_id, "AGE", temp)
        tg_send(chat_id, "¿Qué edad tienes? (ej: 34)")
        return {"ok": True}

    if step == "AGE":
        if not text.isdigit():
            tg_send(chat_id, "Edad inválida. Escribe un número (ej: 34).")
            return {"ok": True}
        temp["age"] = int(text)
        set_state(user_id, "HEIGHT", temp)
        tg_send(chat_id, "¿Cuál es tu altura en cm? (ej: 163)")
        return {"ok": True}

    if step == "HEIGHT":
        if not text.isdigit():
            tg_send(chat_id, "Altura inválida. Escribe un número en cm (ej: 163).")
            return {"ok": True}
        temp["height_cm"] = int(text)
        set_state(user_id, "WEIGHT", temp)
        tg_send(chat_id, "¿Cuál es tu peso en kg? (ej: 67)")
        return {"ok": True}

    if step == "WEIGHT":
        try:
            temp["weight_kg"] = float(text.replace(",", "."))
        except:
            tg_send(chat_id, "Peso inválido. Ej: 67")
            return {"ok": True}
        set_state(user_id, "ACTIVITY", temp)
        tg_send(chat_id, "Nivel de actividad: sedentaria / ligera / moderada / alta")
        return {"ok": True}

    if step == "ACTIVITY":
        lvl = text.strip().lower()
        if lvl not in {"sedentaria", "ligera", "moderada", "alta"}:
            tg_send(chat_id, "Escribe una de estas: sedentaria / ligera / moderada / alta")
            return {"ok": True}
        temp["activity_level"] = lvl
        set_state(user_id, "GOAL", temp)
        tg_send(chat_id, "Objetivo: deficit / mantenimiento / volumen")
        return {"ok": True}

    if step == "GOAL":
        goal = text.strip().lower()
        if goal not in {"deficit", "mantenimiento", "volumen"}:
            tg_send(chat_id, "Escribe: deficit / mantenimiento / volumen")
            return {"ok": True}
        temp["goal"] = goal

        # Completar onboarding llamando a tu función
        try:
            rpc = sb.rpc("complete_onboarding", {
                "p_user_id": user_id,
                "p_sex": temp["sex"],
                "p_age": temp["age"],
                "p_height_cm": temp["height_cm"],
                "p_weight_kg": temp["weight_kg"],
                "p_activity_level": temp["activity_level"],
                "p_goal": temp["goal"],
            }).execute()
            row = rpc.data[0]
            set_state(user_id, "READY", {"meal_type": "ALMUERZO"})
            tg_send(chat_id,
                f"Listo ✅\n"
                f"Tu meta diaria:\n"
                f"Kcal: {row['target_kcal']}\n"
                f"P: {row['target_p']}g | C: {row['target_c']}g | F: {row['target_f']}g\n\n"
                f"Ahora escribe: DESAYUNO/ALMUERZO/CENA/SNACK y luego 'alimento gramos' (ej: pollo cocido 180g)."
            )
        except:
            tg_send(chat_id, "No pude calcular tus macros. Intenta nuevamente.")
        return {"ok": True}

    # READY: registrar comidas
    if upper in MEAL_TYPES:
        set_state(user_id, "READY", {"meal_type": upper})
        tg_send(chat_id, f"Comida actual: {upper}. Ahora envía alimento + gramos.")
        return {"ok": True}

    food, grams = parse_food_grams(text)
    if not food:
        tg_send(chat_id, "Formato no válido. Ej: 'arroz cocido 200g'")
        return {"ok": True}

    meal_type = (temp.get("meal_type") or "ALMUERZO").upper()

    try:
        rpc = sb.rpc("log_food", {
            "p_user_id": user_id,
            "p_meal_type": meal_type,
            "p_food_text": food,
            "p_grams": grams
        }).execute()
        row = rpc.data[0]
        tg_send(chat_id,
            f"✅ {meal_type}: {row['food_name']} {row['grams']}g\n"
            f"+{row['kcal']} kcal | P {row['p']} | C {row['c']} | F {row['f']}\n\n"
            f"📌 Total hoy: {row['day_total_kcal']} kcal | P {row['day_total_p']} | C {row['day_total_c']} | F {row['day_total_f']}"
        )
    except:
        tg_send(chat_id, f"No encontré '{food}'. Prueba otro nombre del catálogo.")
    return JSONResponse(content={"ok": True})
@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()

    # Log para verificar que llegan mensajes
    print("INCOMING UPDATE:", update)

    # Procesar mensaje
    if "message" in update:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"].get("text", "")

        if text == "/start":
            tg_send(chat_id, "Bot activo 🚀")
        else:
            tg_send(chat_id, f"Recibí: {text}")

    return {"ok": True}
