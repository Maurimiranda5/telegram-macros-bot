import os
import re
import datetime as dt
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
GOALS = {"DEFICIT", "VOLUMEN", "MANTENER"}

ACTIVITY_CHOICES = {
    "1.2": "Sedentario",
    "1.375": "Ligera",
    "1.55": "Moderada",
    "1.725": "Alta",
    "1.9": "Muy alta (solo atletas)"
}


# -------------------------
# Helpers Telegram
# -------------------------
def tg_send(chat_id: int, text: str):
    if not TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    except Exception:
        pass


def normalize_food_name(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def parse_food_grams(line: str):
    """
    Acepta:
      - "pollo cocido 180g"
      - "pollo cocido 180"
      - "arroz 200 g"
    Devuelve (food_name, grams) o (None, None)
    """
    t = line.strip()
    if not t:
        return None, None

    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(g|gr|gramos)?\s*$", t, re.IGNORECASE)
    if not m:
        return None, None

    grams = float(m.group(1).replace(",", "."))
    name = t[: m.start()].strip()
    name = normalize_food_name(name)

    if not name or grams <= 0:
        return None, None
    return name, grams


# -------------------------
# Helpers DB (Supabase)
# -------------------------
def get_or_create_user(telegram_user: dict) -> int:
    """
    users: id (serial), telegram_id (unique), display_name
    """
    tg_id = int(telegram_user.get("id", 0))
    display = telegram_user.get("first_name") or telegram_user.get("username") or "Usuario"

    existing = sb.table("users").select("id").eq("telegram_id", tg_id).limit(1).execute()
    if existing.data:
        return int(existing.data[0]["id"])

    ins = sb.table("users").insert({"telegram_id": tg_id, "display_name": display}).execute()
    return int(ins.data[0]["id"])


def get_state(user_id: int):
    res = sb.table("user_state").select("state,data").eq("user_id", user_id).limit(1).execute()
    if not res.data:
        return None, {}
    row = res.data[0]
    return row.get("state"), (row.get("data") or {})


def set_state(user_id: int, state: str, data: dict):
    sb.table("user_state").upsert(
        {"user_id": user_id, "state": state, "data": data},
        on_conflict="user_id"
    ).execute()


def clear_state(user_id: int):
    set_state(user_id, "READY", {})


def call_rpc_safe(fn_name: str, payload: dict):
    try:
        res = sb.rpc(fn_name, payload).execute()
        return True, res.data, None
    except Exception as e:
        return False, None, str(e)


# -------------------------
# UX copy
# -------------------------
WELCOME = (
    "👋 Hola! Soy tu bot de macros.\n\n"
    "Para activar tu acceso, envíame tu código.\n"
    "Ejemplo: MVP-1001"
)

ASK_SEX = "1/6 ¿Eres H (hombre) o M (mujer)? Responde: H o M"
ASK_AGE = "2/6 ¿Qué edad tienes? (solo número)"
ASK_HEIGHT = "3/6 ¿Cuánto mides en cm? Ej: 163"
ASK_WEIGHT = "4/6 ¿Cuánto pesas en kg? Ej: 67"
ASK_ACTIVITY = (
    "5/6 Elige tu nivel de actividad (responde el número):\n"
    "1.2 Sedentario\n"
    "1.375 Ligera\n"
    "1.55 Moderada\n"
    "1.725 Alta\n"
    "1.9 Muy alta (solo atletas)"
)
ASK_GOAL = (
    "6/6 ¿Objetivo?\n"
    "Responde: DEFICIT o VOLUMEN o MANTENER\n"
    "(Para simplificar: DEFICIT=-400 kcal, VOLUMEN=+300 kcal)"
)

READY_HELP = (
    "✅ Listo. Ya tengo tus macros.\n\n"
    "Para registrar comida:\n"
    "1) Primero escribe el tipo: DESAYUNO / ALMUERZO / CENA / SNACK\n"
    "2) Luego envía items como: \"pollo cocido 180g\"\n"
    "   Puedes mandar varios (una línea por item).\n\n"
    "Ejemplo:\n"
    "ALMUERZO\n"
    "pollo cocido 180g\n"
    "arroz 200g\n\n"
    "Comandos:\n"
    "/status  (ver objetivo)\n"
    "/reset   (reiniciar flujo)\n"
)

UNKNOWN_FOOD = (
    "No encontré ese alimento en el catálogo.\n"
    "Prueba con un nombre exacto.\n"
    "Ej: \"pollo cocido 180g\" o \"arroz 200g\""
)


# -------------------------
# Endpoints
# -------------------------
@app.get("/health")
def health():
    return {"ok": True}


@app.post("/webhook")
async def webhook(req: Request):
    update = await req.json()

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return JSONResponse({"ok": True})

    chat_id = int(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()

    from_user = msg.get("from") or {}
    telegram_id = int(from_user.get("id", 0))
    display_name = (
        (from_user.get("first_name") or "")
        + (" " + from_user.get("last_name") if from_user.get("last_name") else "")
    ).strip() or (from_user.get("username") or "Usuario")

    user_id = get_or_create_user(from_user)
    state, data = get_state(user_id)

    if not state:
        state = "NEW"
        data = {}
        set_state(user_id, state, data)

    # comandos
    if text.lower() == "/start":
        set_state(user_id, "AWAIT_CODE", {})
        tg_send(chat_id, WELCOME)
        return JSONResponse({"ok": True})

    if text.lower() == "/reset":
        set_state(user_id, "AWAIT_CODE", {})
        tg_send(chat_id, "🔄 Reiniciado. " + WELCOME)
        return JSONResponse({"ok": True})

    if text.lower() == "/status":
        prof = sb.table("user_profile").select(
            "kcal_target,protein_g,carbs_g,fats_g,goal,activity_factor,weight_kg,height_cm,age,sex"
        ).eq("user_id", user_id).limit(1).execute()
        if not prof.data:
            tg_send(chat_id, "Aún no tengo tu perfil. Escribe /start para comenzar.")
        else:
            p = prof.data[0]
            tg_send(
                chat_id,
                "📌 Tu objetivo:\n"
                f"- Objetivo: {p.get('goal')}\n"
                f"- Kcal: {p.get('kcal_target')}\n"
                f"- Proteína: {p.get('protein_g')} g\n"
                f"- Carbs: {p.get('carbs_g')} g\n"
                f"- Grasas: {p.get('fats_g')} g\n"
            )
        return JSONResponse({"ok": True})

    # flujo principal
    if state in ("NEW",):
        set_state(user_id, "AWAIT_CODE", {})
        tg_send(chat_id, WELCOME)
        return JSONResponse({"ok": True})

    # 1) Activación
    if state == "AWAIT_CODE":
        code = text.strip()

        ok, _, err = call_rpc_safe(
            "activate_with_code",
            {
                "p_code": code,
                "p_display_name": display_name,
                "p_telegram_id": telegram_id,
            },
        )

        if not ok:
            tg_send(chat_id, f"⚠️ No pude validar el código. Intenta de nuevo.\nDetalle: {err}")
            return JSONResponse({"ok": True})

        set_state(user_id, "ONB_SEX", {})
        tg_send(chat_id, "✅ Código válido.\n" + ASK_SEX)
        return JSONResponse({"ok": True})

    # 2) Onboarding
    if state == "ONB_SEX":
        t = text.strip().upper()
        if t not in ("H", "M"):
            tg_send(chat_id, "Responde solo H o M.")
            return JSONResponse({"ok": True})
        data["sex"] = t
        set_state(user_id, "ONB_AGE", data)
        tg_send(chat_id, ASK_AGE)
        return JSONResponse({"ok": True})

    if state == "ONB_AGE":
        if not text.isdigit():
            tg_send(chat_id, "Edad inválida. Responde solo un número (ej: 34).")
            return JSONResponse({"ok": True})
        data["age"] = int(text)
        set_state(user_id, "ONB_HEIGHT", data)
        tg_send(chat_id, ASK_HEIGHT)
        return JSONResponse({"ok": True})

    if state == "ONB_HEIGHT":
        try:
            h = float(text.replace(",", "."))
            if h < 120 or h > 230:
                raise ValueError()
        except Exception:
            tg_send(chat_id, "Altura inválida. Ej: 163")
            return JSONResponse({"ok": True})
        data["height_cm"] = h
        set_state(user_id, "ONB_WEIGHT", data)
        tg_send(chat_id, ASK_WEIGHT)
        return JSONResponse({"ok": True})

    if state == "ONB_WEIGHT":
        try:
            w = float(text.replace(",", "."))
            if w < 30 or w > 250:
                raise ValueError()
        except Exception:
            tg_send(chat_id, "Peso inválido. Ej: 67")
            return JSONResponse({"ok": True})
        data["weight_kg"] = w
        set_state(user_id, "ONB_ACTIVITY", data)
        tg_send(chat_id, ASK_ACTIVITY)
        return JSONResponse({"ok": True})

    if state == "ONB_ACTIVITY":
        key = text.strip()
        if key not in ACTIVITY_CHOICES:
            tg_send(chat_id, "Elige uno de estos: 1.2, 1.375, 1.55, 1.725, 1.9")
            return JSONResponse({"ok": True})
        data["activity_factor"] = float(key)
        set_state(user_id, "ONB_GOAL", data)
        if key == "1.9":
            tg_send(chat_id, "⚠️ 1.9 es para atletas. Si no eres atleta, usa 1.55 o 1.725.\n\n" + ASK_GOAL)
        else:
            tg_send(chat_id, ASK_GOAL)
        return JSONResponse({"ok": True})

    if state == "ONB_GOAL":
        g = text.strip().upper()
        if g not in GOALS:
            tg_send(chat_id, "Responde: DEFICIT o VOLUMEN o MANTENER")
            return JSONResponse({"ok": True})
        data["goal"] = g

        ok, _, err = call_rpc_safe(
            "complete_onboarding",
            {
                "p_user_id": user_id,
                "p_sex": data["sex"],
                "p_age": int(data["age"]),
                "p_height_cm": float(data["height_cm"]),
                "p_weight_kg": float(data["weight_kg"]),
                "p_activity_factor": float(data["activity_factor"]),
                "p_goal": data["goal"],
            },
        )
        if not ok:
            tg_send(chat_id, f"⚠️ No pude calcular tus macros.\nDetalle: {err}")
            return JSONResponse({"ok": True})

        clear_state(user_id)
        tg_send(chat_id, READY_HELP)
        return JSONResponse({"ok": True})

    # 3) Registro comidas
    if state == "READY":
        t = text.strip().upper()
        if t in MEAL_TYPES:
            set_state(user_id, "READY_MEAL_SELECTED", {"meal_type": t})
            tg_send(chat_id, f"✅ Ok. Envíame los items de {t} (uno por línea). Ej: pollo cocido 180g")
            return JSONResponse({"ok": True})

        tg_send(chat_id, "Primero dime el tipo: DESAYUNO / ALMUERZO / CENA / SNACK")
        return JSONResponse({"ok": True})

    if state == "READY_MEAL_SELECTED":
        meal_type = (data or {}).get("meal_type")
        if not meal_type:
            set_state(user_id, "READY", {})
            tg_send(chat_id, "Se perdió el tipo de comida. Escribe: DESAYUNO / ALMUERZO / CENA / SNACK")
            return JSONResponse({"ok": True})

        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not lines:
            tg_send(chat_id, "Envíame al menos 1 item. Ej: arroz 200g")
            return JSONResponse({"ok": True})

        today = dt.date.today().isoformat()
        success = 0
        fails = 0

        for line in lines:
            food_name, grams = parse_food_grams(line)
            if not food_name:
                fails += 1
                continue

            ok, _, _ = call_rpc_safe(
                "log_food",
                {
                    "p_user_id": user_id,
                    "p_day": today,
                    "p_meal_type": meal_type,
                    "p_food_name": food_name,
                    "p_grams": grams,
                },
            )
            if ok:
                success += 1
            else:
                fails += 1

        if success == 0:
            tg_send(chat_id, UNKNOWN_FOOD)
        else:
            msg = f"✅ Registré {success} item(s) en {meal_type}."
            if fails:
                msg += f"\n⚠️ {fails} no pude registrarlos (formato o alimento no encontrado)."
            msg += "\n\n¿Quieres registrar más? (o escribe otro tipo: DESAYUNO/ALMUERZO/CENA/SNACK)"
            tg_send(chat_id, msg)

        set_state(user_id, "READY", {})
        return JSONResponse({"ok": True})

    tg_send(chat_id, "Escribe /start para comenzar.")
    return JSONResponse({"ok": True})
