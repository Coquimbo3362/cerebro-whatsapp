import os
import requests
import json
import time
import traceback
import re
from flask import Flask, request
from twilio.rest import Client as TwilioClient 
from twilio.twiml.messaging_response import MessagingResponse
from supabase import create_client, Client
from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image
import io
import gc

# --- CONFIGURACIÓN ---
load_dotenv()
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GOOGLE_KEY = os.environ.get("GOOGLE_API_KEY")
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")

# Verificación rápida de claves al arrancar
if not all([SUPABASE_URL, SUPABASE_KEY, GOOGLE_KEY, TWILIO_SID, TWILIO_TOKEN]):
    print("❌ ERROR CRÍTICO: Faltan variables de entorno en Render.")

app = Flask(__name__)

# Clientes
try:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    client = genai.Client(api_key=GOOGLE_KEY)
    twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN) 
    MODELO_IA = 'gemini-2.5-flash'
except Exception as e:
    print(f"Error iniciando clientes: {e}")

# --- FUNCIONES DE LIMPIEZA ---
def limpiar_numero(valor):
    if not valor: return 0.0
    texto = str(valor).replace('$', '').strip()
    texto = re.sub(r'[^\d.,]', '', texto) 
    try: return float(texto.replace(',', '.')) 
    except: return 0.0

def limpiar_fecha(fecha_str):
    return fecha_str if fecha_str and len(fecha_str) == 10 else time.strftime("%Y-%m-%d")

def optimizar_imagen(imagen_bytes):
    try:
        img = Image.open(io.BytesIO(imagen_bytes))
        # Reducimos tamaño para ahorrar RAM
        if img.width > 800 or img.height > 800:
            img.thumbnail((800, 800))
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format='JPEG', quality=60)
        return img_byte_arr.getvalue()
    except:
        return imagen_bytes

# --- LÓGICA DE IA ---
def procesar_ticket_ia(lista_archivos):
    contenidos = ["""
    Analiza este ticket. Extrae: Supermercado, Total, Fecha (YYYY-MM-DD), y una lista de productos.
    Para cada producto: nombre, cantidad, precio_neto_final, rubro (de la lista), marca, codigo_barras (si existe).
    Rubros: Almacén, Bebidas, Carnicería, Verdulería, Lácteos, Limpieza, Farmacia, Otros.
    JSON Estricto: {"supermercado": "str", "total_pagado": num, "fecha": "YYYY-MM-DD", "items": [{"nombre": "str", "codigo_barras": "str", "cantidad": num, "precio_neto_final": num, "rubro": "str", "marca": "str"}]}
    """]
    
    for archivo in lista_archivos:
        contenidos.append(types.Part.from_bytes(data=archivo['bytes'], mime_type=archivo['mime']))
    
    response = client.models.generate_content(
        model=MODELO_IA,
        contents=contenidos,
        config=types.GenerateContentConfig(response_mime_type='application/json')
    )
    return json.loads(response.text)

# --- WEBHOOK ---
@app.route("/whatsapp", methods=['POST'])
def whatsapp_webhook():
    print("📩 Recibido webhook de Twilio")
    try:
        sender = request.form.get('From', '')
        telefono = sender.replace("whatsapp:", "")
        num_media = int(request.form.get('NumMedia', 0))
        
        # Identificar usuario
        res = supabase.table('perfiles').select("id").eq('telefono', telefono).execute()
        if not res.data:
            return "No registrado", 200

        user_id = res.data[0]['id']

        if num_media > 0:
            archivos = []
            for i in range(num_media):
                url = request.form.get(f'MediaUrl{i}')
                mime = request.form.get(f'MediaContentType{i}')
                r = requests.get(url, auth=(TWILIO_SID, TWILIO_TOKEN))
                if r.status_code == 200:
                    data = r.content if "pdf" in mime else optimizar_imagen(r.content)
                    archivos.append({'bytes': data, 'mime': mime})
            
            datos = procesar_ticket_ia(archivos)
            
            # Guardar Ticket
            nombre_super = datos.get('supermercado', 'DESCONOCIDO').upper()
            res_super = supabase.table('supermercados').select('id').ilike('nombre', nombre_super).execute()
            super_id = res_super.data[0]['id'] if res_super.data else supabase.table('supermercados').insert({"nombre": nombre_super}).execute().data[0]['id']

            ticket_data = {"user_id": user_id, "supermercado_id": super_id, "fecha": limpiar_fecha(datos.get('fecha')), "monto_total": limpiar_numero(datos.get('total_pagado')), "imagen_url": "v5.2"}
            ticket_id = supabase.table('tickets').insert(ticket_data).execute().data[0]['id']

            items = [{**it, "ticket_id": ticket_id, "nombre_producto": it.get('nombre'), "cantidad": limpiar_numero(it.get('cantidad')), "precio_neto_unitario": limpiar_numero(it.get('precio_neto_final'))} for it in datos.get('items', [])]
            if items: supabase.table('items_compra').insert(items).execute()
            
            # Respuesta activa
            twilio_client.messages.create(from_='whatsapp:+14155238886', to=sender, body=f"✅ Guardado: {nombre_super} (${datos.get('total_pagado')})")
        
    except Exception as e:
        print(f"🔴 ERROR CRÍTICO: {traceback.format_exc()}")
        
    return str(MessagingResponse())

if __name__ == "__main__":
    app.run(port=5000)