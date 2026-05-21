FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir google-genai requests
COPY organizer.py .
CMD ["python", "organizer.py"]
