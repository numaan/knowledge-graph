import express from "express";
import { prisma } from "./db";
import { fetchOrder } from "./orders";

export const router = express.Router();

export async function createInvoice(req: express.Request, res: express.Response) {
  const order = await fetchOrder(req.body.orderId);
  const invoice = await prisma.invoice.create({ data: { orderId: order.id, total: 0 } });
  res.status(201).json(invoice);
}

export async function getInvoice(req: express.Request, res: express.Response) {
  res.json(await prisma.invoice.findUnique({ where: { id: Number(req.params.id) } }));
}

router.post("/invoices", createInvoice);
router.get("/invoices/:id", getInvoice);
router.get("/health", (req, res) => res.send("ok"));
