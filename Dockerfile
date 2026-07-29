FROM python:3.10-slim

LABEL maintainer="youwei.wang@shopee.com"
LABEL description="Multi-Project GPU Monitor & SeaTalk Alert Service"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://pypi.garenanow.com/simple/ \
    --extra-index-url https://pypi.org/simple/

# Copy entire project so container mirrors local structure
COPY . .

CMD ["python3", "aigc_gpu_alert.py", "--loop", "--verify"]
