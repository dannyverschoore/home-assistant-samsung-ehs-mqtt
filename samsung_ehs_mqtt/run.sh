#!/usr/bin/with-contenv bashio

export SERIAL_PORT="$(bashio::config 'serial_port')"
export MQTT_HOST="$(bashio::config 'mqtt_host')"
export MQTT_PORT="$(bashio::config 'mqtt_port')"
export MQTT_USER="$(bashio::config 'mqtt_user')"
export MQTT_PASSWORD="$(bashio::config 'mqtt_password')"
export PRINT_VALUES="$(bashio::config 'print_values')"
export HEARTBEAT_SECONDS="$(bashio::config 'heartbeat_seconds')"
export REPUBLISH_SECONDS="$(bashio::config 'republish_seconds')"

echo "Samsung EHS MQTT add-on gestart"
echo "Serial port: ${SERIAL_PORT}"
echo "MQTT host: ${MQTT_HOST}:${MQTT_PORT}"

python3 -u /samsung_ehs_mqtt.py
