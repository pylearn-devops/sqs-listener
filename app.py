# app.py
from src.sqs_fargate_listener import sqs_listener, run_listeners
from src.sqs_fargate_listener.types import SqsMessage, BatchResult
import time


# ------------------------------------------------------------------
# Example 1: Batch mode (processes up to 10 messages together)
# ------------------------------------------------------------------
@sqs_listener(
    # You can omit this and pass QUEUE_URL via environment variable
    # queue_url="https://sqs.us-east-1.amazonaws.com/123456789/test-queue",
    mode="batch",             # can be "batch" or "per_message"
    wait_time=20,             # long polling wait time
    batch_size=10,            # max messages per batch
    visibility_secs=60,       # message visibility timeout
    worker_threads=2,         # threads for polling
)
def handle_batch(messages: list[SqsMessage]) -> BatchResult:
    failed = []

    for msg in messages:
        try:
            # Try parsing message as JSON
            data, err = msg.try_json()
            if err:
                print(f"[WARN] Invalid JSON: {err}")
                failed.append(msg.receipt_handle)
                continue

            # Simulate processing
            print(f"[INFO] Processing message {msg.message_id}: {data}")
            time.sleep(1)  # simulate some work

        except Exception as e:
            print(f"[ERROR] Failed to process {msg.message_id}: {e}")
            failed.append(msg.receipt_handle)

    # Return failed messages (these won't be deleted)
    return BatchResult(failed_receipt_handles=failed)


# ------------------------------------------------------------------
# Example 2: Per-message mode (one message at a time)
# ------------------------------------------------------------------
@sqs_listener(mode="per_message")
def handle_single(msg: SqsMessage) -> bool:
    """
    Return True → delete message
    Return False or raise → keep message (for retry/DLQ)
    """
    try:
        data = msg.json  # parse JSON body
        print(f"[INFO] Got single message: {data}")
        # Simulate some work
        time.sleep(0.5)
        return True
    except Exception as e:
        print(f"[ERROR] Failed to process single message: {e}")
        return False


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------
if __name__ == "__main__":
    print("[BOOT] Starting SQS listeners...")
    run_listeners()  # Starts all registered listeners and blocks
