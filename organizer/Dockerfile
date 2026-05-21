FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir google-genai python-dotenv
COPY ../lib.py .
COPY ../notify.py .
COPY organizer.py .
CMD ["python", "organizer.py"]
