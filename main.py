import os
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI()

@app.get("/")
def home():
    return {"status": "Bot running"}

@app.post("/telegram/webhook")
async def telegram_webhook():
    return JSONResponse(content={"ok": True})
