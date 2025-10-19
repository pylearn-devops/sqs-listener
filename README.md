# 🦾 sqs-fargate-listener

> **A lightweight, Lambda-like SQS listener for Python running on ECS Fargate (or any container).**  
> Handles long-polling, visibility heartbeats, partial batch failure, graceful shutdown, and beautiful colorized logs out of the box.



## 🚀 Why this exists

When you attach an **SQS** queue to **AWS Lambda**, AWS automatically manages:
  - polling the queue
  - invoking your handler
  - deleting successful messages
  - redriving failures.

On **ECS Fargate**, you don’t get this for free — you have to build a poller, manage visibility timeouts, handle retries, and scale your service yourself.

This package brings the same convenience to containers. Just decorate a function with `@sqs_listener(...)`, and the library does the rest.


### 🧩 Install

```bash
pip install sqs-fargate-listener
```

---

## ✨ Quick Start

### 💡 Processing Messages in `app.py`

When you decorate a function with `@sqs_listener(...)`, you’re telling the library:

> “Whenever messages arrive in this queue, call this function with them.”

The decorator automatically:
- polls the SQS queue,
- extends the visibility timeout while you work,
- invokes your function,
- deletes successful messages,
- and retries or sends failures to the DLQ.

**There are two handler styles you can use depending on your needs:**

### 🧩 1. Batch mode (recommended for throughput)

Batch mode receives a *list* of messages (up to `BATCH_SIZE`, default 10) and lets you mark which ones failed.

```python
# app.py
from sqs_fargate_listener import sqs_listener, run_listeners
from sqs_fargate_listener.types import SqsMessage, BatchResult

@sqs_listener(
    queue_url="https://sqs.us-east-1.amazonaws.com/123/orders-queue",
    mode="batch",
    batch_size=10,
)
def handle_orders(messages: list[SqsMessage]) -> BatchResult:
    failed = []

    for msg in messages:
        try:
            # Parse message as JSON (cached after first use)
            data = msg.json
            print(f"🛒 Processing order {data['order_id']} for {data['customer']}")
            
            # do your logic here (save to DB, call APIs, etc.)
            process_order(data)
        
        except Exception as e:
            print(f"❌ Failed to process {msg.message_id}: {e}")
            failed.append(msg.receipt_handle)
    
    # Only successful messages will be deleted
    return BatchResult(failed_receipt_handles=failed)

if __name__ == "__main__":
    run_listeners()
```

- ✅ **Advantages:**
  * fewer SQS API calls, better throughput
  * partial batch failure supported
  * automatic retries by SQS DLQ policy

### 🪄 2. Per-message mode (simple boolean handler)

**Per-message mode gives you one SqsMessage at a time and expects a boolean return:**
  * True → delete the message
  * False or exception → leave in queue for retry

```python
@sqs_listener(
    queue_url="https://sqs.us-east-1.amazonaws.com/123/email-queue",
    mode="per_message",
    worker_threads=4,
)
def handle_email(msg: SqsMessage) -> bool:
    data, err = msg.try_json()
    if err:
        print(f"Invalid JSON: {err}")
        return False

    print(f"📧 Sending email to {data['recipient']} ...")
    try:
        send_email(data)
        return True
    except Exception as e:
        print(f"Send failed: {e}")
        return False
```
> [!NOTE]
> **✅ Simpler to reason about. Best when each message is independent and quick to process.**

## 📦 Working with message attributes

**You can access message attributes attached to the SQS message:**

```python
attrs = msg.message_attributes()
trace_id = attrs.get("trace_id")
print(f"Processing message trace_id={trace_id}")
```

**Make sure your queue’s producer sets attributes when sending messages:**

```python
sqs.send_message(
	QueueUrl=queue_url,
	MessageBody=json.dumps({...}),
	MessageAttributes={
		"trace_id": {"DataType": "String", "StringValue": "abc-123"}
	}
)
```


### 🧩 SqsMessage helpers

| Property / Method      | Description                                 |
|------------------------|---------------------------------------------|
| .message_id            | SQS message ID                              |
| .body                  | Raw message body (string)                   |
| .json                  | Cached parsed JSON (raises if invalid)      |
| .try_json()            | Returns (data, error) safely                |
| .message_attributes()  | Simplified dict of SQS MessageAttributes    |
| .receipt_handle        | Internal SQS handle (used for deletion)     |


### 🧰 Example: multiple queues in one app

```python
from sqs_fargate_listener import sqs_listener, run_listeners
from sqs_fargate_listener.types import SqsMessage

@sqs_listener(queue_url="https://…/payments-queue", mode="batch")
def process_payments(msgs): ...

@sqs_listener(queue_url="https://…/notifications-queue", mode="per_message")
def send_notifications(msg): ...

if __name__ == "__main__":
    run_listeners()
```

> [!NOTE]
> **Both queues are polled concurrently, each on its own threads.**

### 🛡️ Error handling & retries
- Uncaught exceptions or return False → message not deleted → retried later.
- After maxReceiveCount (queue setting) → message sent to DLQ.
- You can safely raise any exception; it’s caught by the listener.

For longer jobs, visibility is automatically extended while your handler runs.

### ⚙️ How it works

- **Each decorated function spawns one or more polling threads:**
	1.	Long-polls SQS (WaitTimeSeconds=20 by default).
	2.	Receives a batch (up to 10 messages).
	3.	Extends visibility timeout while processing (heartbeat thread).
	4.	Invokes your handler in either batch or per-message mode.
	5.	Deletes successfully processed messages.
	6.	Keeps failed ones for retry / DLQ.
	7.	Gracefully drains on SIGTERM (during ECS scale-in or container stop).

> [!NOTE]
> **Everything runs in-process with no need for AWS Lambda or additional services.**

### 🪶 Features at a glance

| Feature                   | Description                                     |
|---------------------------|-------------------------------------------------|
| Decorator-based API       | Register multiple handlers with \@sqs_listener. |
| Batch & per-message modes | Choose your processing style.                   |
| Automatic long-polling    | Efficient queue reads (WaitTimeSeconds=20).     |
| Visibility heartbeat      | Prevents double processing during long jobs.    |
| Partial batch failure     | Delete successes, leave failed messages.        |
| Graceful shutdown         | Finishes in-flight work on SIGTERM.             |
| Thread-safe concurrency   | Multiple pollers per queue.                     |
| Color-aware logging       | Color in TTY, plain in CloudWatch.              |
| Fully configurable        | Override via decorator args or env vars.        |


---

## 🧠 Configuration

Decorator Arguments

```python
@sqs_listener(
  queue_url="...",
  mode="batch",
  wait_time=10,
  batch_size=5,
  visibility_secs=45,
  max_extend=600,
  worker_threads=8,
)
```

## Environment variables

| Variable        | Default | Description                        |
|-----------------|---------|------------------------------------|
| WAIT_TIME       | 20      | SQS long-poll seconds              |
| BATCH_SIZE      | 10      | Max messages per poll              |
| VISIBILITY_SECS | 60      | Initial visibility timeout         |
| MAX_EXTEND      | 900     | Max total visibility extension     |
| WORKER_THREADS  | 4       | Poller threads per listener        |
| IDLE_SLEEP_MAX  | 2.0     | Max random sleep after empty poll  |


> [!NOTE]
> Environment variables are overridden by decorator arguments. Precedence: Decorator > Env > Default

---

## 🪵 Logging

The package uses colorlog for vivid, structured output that’s CloudWatch-safe.

Environmental controls:

| Variable         | Default                                           | Description                                  |
|------------------|---------------------------------------------------|----------------------------------------------|
| LOG_LEVEL        | INFO                                              | One of DEBUG, INFO, WARN, ERROR              |
| LOG_USE_COLOR    | 1                                                 | Use ANSI colors (auto-disabled if not a TTY) |
| LOG_FORMAT       | colored pattern                                   | Colored log format                           |
| LOG_PLAIN_FORMAT | [%(levelname)s] %(message)s (%(name)s:%(lineno)d) | Used in non-TTY mode                         |
| LOG_DATEFMT      | %Y-%m-%d %H:%M:%S                                 | Timestamp format                             |


## Examples

**Local Dev**

```bash
LOG_LEVEL=DEBUG python app.py
```

**CloudWatch / ECS:**

```bash
LOG_USE_COLOR=0 python app.py
```

> [!NOTE]
> The logger automatically detects non-TTY environments and switches to plain text for clean CloudWatch logs.

---

## 🧰 IAM Permissions

The task role (or instance role) needs these actions on your queue:

```json
{
  "Effect": "Allow",
  "Action": [
    "sqs:ReceiveMessage",
    "sqs:DeleteMessage",
    "sqs:ChangeMessageVisibility",
    "sqs:GetQueueAttributes"
  ],
  "Resource": "arn:aws:sqs:REGION:ACCOUNT_ID:QUEUE_NAME"
}
```

## 🧭 Deploying on ECS Fargate

- Build a Docker image with your app and this package installed.

	```dockerfile
	FROM python:3.11-slim
	WORKDIR /app
	COPY . .
	RUN pip install .
	CMD ["python", "app.py"]
	```

- Create a Task Definition:
  * Use your queue’s QUEUE_URL as an environment variable.
  * Add your AWS permissions via IAM task role.
  * Set stopTimeout to ≥ 60 seconds for graceful drains.
  
- Run a Service on Fargate:
  * Launch at least one replica.
  * Enable autoscaling based on SQS metrics (ApproximateNumberOfMessagesVisible).

## ⚡️ Autoscaling tips

**Use target tracking on queue depth per task:**

| Metric                                            | Example Target       |
|---------------------------------------------------|----------------------|
| ApproximateNumberOfMessagesVisible / DesiredCount | 10 messages per task |


> [!NOTE]
> Scale out when backlog > target, in when < target.

---

## 🧱 How it differs from AWS Lambda + SQS


| Capability            | AWS Lambda              | sqs-fargate-listener    |
|-----------------------|-------------------------|-------------------------|
| Managed polling       | ✅                       | ✅ (inside container)    |
| Pay-per-invocation    | ✅                       | ❌ (you manage tasks)    |
| Concurrency autoscale | ✅                       | via ECS autoscaling     |
| Partial batch failure | ✅                       | ✅                       |
| Visibility heartbeat  | ✅                       | ✅                       |
| Code model            | def handler(event, ctx) | @sqs_listener decorator |
| Local debugging       | Limited                 | ✅                       |

---

## 🔒 Graceful shutdown behavior

**When Fargate stops a task, it sends SIGTERM.**

- **The listener:**
  * Stops fetching new messages.
  * Waits for active handlers to finish.
  * Extends visibility if needed.
  * Exits cleanly.

Set ECS container stopTimeout ≥ your typical processing time.

---


## 🧪 Testing locally

- **Quick Test Setup:**

  * Start LocalStack and create queue: `make localstack queue`
  * Full e2e flow: `make test` (starts LocalStack, creates queue, runs worker, sends message)

- **Individual Testing Steps:**

  * Launch infrastructure: `make up` (starts LocalStack + worker)
  * Send test message: `make send`
  * Monitor processing: `make logs`
  * Cleanup: `make down`

- **Configuration:**

  * Default queue: test-queue in us-east-1
  * LocalStack endpoint: http://localhost:4566
  * Customize with env vars: REGION, ENDPOINT, QUEUE_NAME

> [!NOTE]
> Make sure `docker` and `aws` CLI are installed and configured in your local.

You can use [LocalStack](https://localstack.cloud) or AWS’s [SQS mock](https://docs.aws.amazon.com/cli/latest/reference/sqs/) for local queues.

### End-to-end test example

```bash
#!/bin/zsh
 docker compose up -d localstack
 aws --endpoint-url=http://localhost:4566 sqs create-queue --queue-name test-queue --region us-east-1
export QUEUE_URL="http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000/test-queue"
docker compose up --build
```

---

## 🧾 License

**MIT © 2025 AB**
