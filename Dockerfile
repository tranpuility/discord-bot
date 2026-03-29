FROM python:3.11

WORKDIR /app

COPY . .
COPY onglefont.ttf /app/onglefont.ttf

RUN ls -al /app
RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "bot.py"]