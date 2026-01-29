FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY examples/ examples/

CMD ["python", "-m", "examples.04_customer_service_demo.main"]
