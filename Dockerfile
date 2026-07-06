ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache python3 py3-pip

RUN pip3 install --break-system-packages pyserial paho-mqtt pysamsungnasa

COPY run.sh /
COPY samsung_ehs_mqtt.py /

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
