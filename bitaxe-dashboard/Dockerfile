FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends sshpass openssh-client && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir flask

COPY bitaxe_dashboard.py /app/
COPY Documents/JSON /app/Documents/JSON/
WORKDIR /app

EXPOSE 5050

CMD ["python", "bitaxe_dashboard.py", "--host", "0.0.0.0", "--port", "5050", "--refresh", "30"]
