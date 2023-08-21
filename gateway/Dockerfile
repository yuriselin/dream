FROM python:3.11
RUN pip install pipenv
RUN mkdir /src
WORKDIR /src
COPY Pipfile Pipfile.lock ./
RUN pipenv install --system --deploy
COPY run.py ./run.py
CMD python ./run.py