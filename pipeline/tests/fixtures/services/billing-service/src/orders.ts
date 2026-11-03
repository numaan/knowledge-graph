import axios from "axios";

const ORDERS_URL = process.env.ORDERS_URL ?? "http://orders:8000";

export async function fetchOrder(id: string) {
  const r = await axios.get(`${ORDERS_URL}/api/orders/${id}`);
  return r.data;
}
