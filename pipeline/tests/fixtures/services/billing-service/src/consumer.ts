import { Kafka } from "kafkajs";

const consumer = new Kafka({ brokers: ["kafka:9092"] }).consumer({ groupId: "billing" });

export async function startConsumer() {
  await consumer.subscribe({ topic: "order-created" });
}
