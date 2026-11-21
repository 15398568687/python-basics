FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 APP_DB=/app/data/console.sqlite3
RUN useradd --system --create-home appuser && mkdir /app/data && chown appuser /app/data
COPY app/ app/
COPY web/ web/
COPY report.py .
USER appuser
EXPOSE 8080
CMD ["python","-m","app","serve","--host","0.0.0.0"]
