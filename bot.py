import os
import requests
import json
import time
import traceback
import re
from flask import Flask, request
# Importamos el Cliente para enviar mensajes activos
from twilio.rest import Client as TwilioClient 
from twilio.twiml.messaging_response import MessagingResponse
from supabase import create_client, Client
from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image
import io

# --- CONFIGURACIÓN ---
load_dotenv()
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GOOGLE_KEY = os.environ.get("GOOGLE_API_KEY")
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")

app = Flask(__name__)

# Clientes
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    client = genai.Client(api_key=GOOGLE_KEY)
    # Cliente para enviar mensajes activos
    twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN) 
    MODELO_IA = 'gemini-2.5-flash'
except Exception as e:
    print(f"Error config: {e}")

# --- FUNCIONES ---
def limpiar_numero(valor):
    if not valor: return 0.0
    texto = str(valor).replace('$', '').strip()
    texto = re.sub(r'[^\d.,]', '', texto) 
    try: return float(texto.replace(',', '.')) 
    except: return 0.0

def limpiar_fecha(fecha_str):
    return fecha_str if fecha_str and len(fecha_str) == 10 else time.strftime("%Y-%m-%d")

def optimizar_imagen(imagen_bytes):
    img = Image.open(io.BytesIO(imagen_bytes))
    if img.width > 1024 or img.height > 1024:
        img.thumbnail((1024, 1024))
    return img

RUBROS_VALIDOS = "Almacén, Bebidas, Carnicería, Verdulería, Lácteos, Limpieza, Otros"

def procesar_imagen_con_ia(imagen_bytes):
    image_pil = optimizar_imagen(imagen_bytes)
    
    prompt = f"""
    Lee este ticket.
    1. Supermercado y Sucursal.
    2. Total Pagado.
    3. LISTA DE PRODUCTOS: Nombre, Cantidad, Precio Unitario.
    Rubros: {RUBROS_VALIDOS}.
    
    JSON: {{
        'supermercado': 'str', 
        'total_pagado': num, 
        'fecha': 'YYYY-MM-DD', 
        'items': [
            {{'nombre': 'str', 'cantidad': num, 'precio_neto_final': num, 'rubro': 'str', 'marca': 'str'}}
        ]
    }}
    """
    
    response = client.models.generate_content(
        model=MODELO_IA,
        contents=[prompt, image_pil],
        config=types.GenerateContentConfig(response_mime_type='application/json')
    )
    return json.loads(response.text)

def guardar_ticket(data, user_id):
    nombre_super = data.get('supermercado', 'Desconocido').upper()
    res_super = supabase.table('supermercados').select('id').ilike('nombre', nombre_super).execute()
    if res_super.data: super_id = res_super.data[0]['id']
    else:
        res_new = supabase.table('supermercados').insert({"nombre": nombre_super}).execute()
        super_id = res_new.data[0]['id']

    ticket_data = {
        "user_id": user_id,
        "supermercado_id": super_id,
        "fecha": limpiar_fecha(data.get('fecha')),
        "hora": "12:00:00",
        "monto_total": data.get('total_pagado', 0),
        "imagen_url": "whatsapp_bot"
    }
    res_ticket = supabase.table('tickets').insert(ticket_data).execute()
    ticket_id = res_ticket.data[0]['id']

    items_brutos = data.get('items', [])
    items_db = []
    for item in items_brutos:
        items_db.append({
            "ticket_id": ticket_id,
            "nombre_producto": item.get('nombre', 'Sin Nombre'),
            "cantidad": item.get('cantidad', 1),
            "precio_neto_unitario": item.get('precio_neto_final', 0),
            "unidad_medida": "Un",
            "rubro": item.get('rubro'),
            "marca": item.get('marca')
        })
    
    if items_db:
        supabase.table('items_compra').insert(items_db).execute()
        return len(items_db)
    return 0

# --- FUNCIÓN DE ENVÍO ACTIVO ---
def enviar_whatsapp(to_number, body_text):
    """Envía un mensaje nuevo, no responde al anterior"""
    try:
        # El número 'from_' es el del Sandbox de Twilio (generalmente fijo)
        # Lo sacamos de la variable de entorno o usamos el estándar
        twilio_client.messages.create(
            from_='whatsapp:+14155238886', 
            body=body_text,
            to=f'whatsapp:{to_number}'
        )
        print("📤 Mensaje de confirmación enviado.")
    except Exception as e:
        print(f"❌ Error enviando WhatsApp: {e}")

# --- WEBHOOK ---
@app.route("/whatsapp", methods=['POST'])
def whatsapp_webhook():
    # Respondemos vacío rápido para que WhatsApp no de timeout
    # Usaremos el envío activo para contestar de verdad
    
    try:
        sender = request.form.get('From')
        media_url = request.form.get('MediaUrl0')
        telefono_usuario = sender.replace("whatsapp:", "")
        
        print(f"1. Procesando para: {telefono_usuario}")

        # Identificar Usuario
        res = supabase.table('perfiles').select("id").eq('telefono', telefono_usuario).execute()
        if not res.data:
            enviar_whatsapp(telefono_usuario, "⛔ No te reconozco. Regístrate en la web primero.")
            return str(MessagingResponse()) # Fin
        
        user_id = res.data[0]['id']

        if media_url:
            # Avisamos que empezamos (opcional)
            # enviar_whatsapp(telefono_usuario, "⏳ Procesando foto...")
            
            print("2. Descargando...")
            r = requests.get(media_url, auth=(TWILIO_SID, TWILIO_TOKEN))
            
            if r.status_code == 200:
                print("3. Analizando con IA...")
                datos = procesar_imagen_con_ia(r.content)
                
                if datos:
                    print("4. Guardando...")
                    cant = guardar_ticket(datos, user_id)
                    total = datos.get('total_pagado')
                    
                    if cant > 0:
                        print("5. ¡ÉXITO!")
                        # AQUÍ ENVIAMOS EL MENSAJE FINAL ACTIVAMENTE
                        enviar_whatsapp(telefono_usuario, f"✅ *Guardado*\n📍 {datos.get('supermercado')}\n💰 ${total}\n🛒 {cant} items")
                    else:
                        enviar_whatsapp(telefono_usuario, "⚠️ Ticket guardado pero vacío.")
                else:
                    enviar_whatsapp(telefono_usuario, "❌ La IA no pudo leer el ticket.")
            else:
                enviar_whatsapp(telefono_usuario, "❌ Error descargando imagen.")
        else:
            enviar_whatsapp(telefono_usuario, "📸 Mándame una foto del ticket.")

    except Exception as e:
        print(f"🔴 ERROR: {e}")
        traceback.print_exc()

    # Retornamos respuesta vacía al servidor de WhatsApp para cumplir protocolo
    return str(MessagingResponse())

if __name__ == "__main__":
    app.run(debug=True, port=5000)