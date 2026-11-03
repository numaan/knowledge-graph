import request from "supertest";
import { app } from "../src/app";

describe("invoices", () => {
  it("creates an invoice", async () => {
    const res = await request(app).post("/invoices").send({ orderId: 1 });
    expect(res.status).toBe(201);
  });
});
