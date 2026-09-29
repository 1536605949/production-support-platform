from locust import HttpUser, task, between
class SupportUser(HttpUser):
    wait_time=between(1,3)
    @task(4)
    def knowledge(self): self.client.post('/api/chat',headers={'X-Demo-User':'user001'},json={'message':'退款政策是多少？'})
    @task(1)
    def business(self): self.client.post('/api/chat',headers={'X-Demo-User':'user001'},json={'message':'查询订单 ORD-1001 状态'})
