from fastapi import APIRouter

from app.routers import orders

api_router = APIRouter(prefix="/api")
api_router.include_router(orders.router)
