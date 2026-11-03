from fastapi import APIRouter, Depends

from app import billing, events, repository
from app.db import get_db

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post("", status_code=201)
def create_order(body: dict, db=Depends(get_db)):
    order = repository.save_order(db, body)
    billing.create_invoice(order)
    events.publish_order_created(order)
    return order


@router.get("/{order_id}")
def get_order(order_id: int, db=Depends(get_db)):
    return repository.load_order(db, order_id)
