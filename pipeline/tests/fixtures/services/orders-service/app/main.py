from fastapi import FastAPI

from app.routers import api_router

app = FastAPI(title="orders")
app.include_router(api_router)
