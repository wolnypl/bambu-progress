FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bambu_progress.py ./

# The container is deliberately stateless. Pass all configuration at runtime
# through environment variables (for example: docker run -e BAMBU_USER_ID=...).
ENTRYPOINT ["python", "bambu_progress.py"]
