from kafka import KafkaProducer

producer = KafkaProducer(bootstrap_servers="kafka:9092")


def publish_order_created(order):
    producer.send("order-created", {"id": order.id})
