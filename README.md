# Samsung EHS MQTT

Home Assistant add-on voor Samsung EHS F1/F2 uitlezing via USB-RS485 en MQTT Discovery.

## Instellingen

- `serial_port`: vaste by-id USB poort
- `mqtt_host`: meestal `core-mosquitto`
- `mqtt_port`: meestal `1883`
- `mqtt_user`: MQTT gebruiker
- `mqtt_password`: MQTT wachtwoord

Na starten worden sensoren automatisch via MQTT Discovery aangemaakt.
