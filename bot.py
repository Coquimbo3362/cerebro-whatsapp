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
    try:
        img = Image.open(io.BytesIO(imagen_bytes))
        if img.width > 1024 or img.height > 1024:
            img.thumbnail((1024, 1024))
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format=img.format)
        return img_byte_arr.getvalue()
    except:
        return imagen_bytes

# --- LISTA DE RUBROS ACTUALIZADA (Con Farmacia) ---
RUBROS_VALIDOS = """
Almacén, Bebidas, Carnicería, Verdulería, Lácteos, Limpieza, 
Perfumería, Farmacia, Mascotas, Indumentaria, Electro, Otros
"""

def procesar_archivo_ia(contenido_bytes, tipo_mime):
    # Si es imagen, la optimizamos. Si es PDF, va crudo.
    datos_para_enviar = contenido_bytes
    if "image" in tipo_mime:
        datos_para_enviar = optimizar_imagen(contenido_bytes)
    
    prompt = f"""
    Lee este ticket o factura.
    1. Comercio y Sucursal.
    2. Total Pagado.
    3. LISTA DE PRODUCTOS: Nombre, Cantidad, Precio Unitario.
    
    IMPORTANTE: Clasifica cada item usando estos Rubros: {RUBROS_VALIDOS}.
    Si son remedios, usa "Farmacia".
    
    JSON: {{
        'supermercado': 'str', 
        'total_pagado': num, 
        'fecha': 'YYYY-MM-DD', 
        'items': [
            {{'nombre': 'str', 'cantidad': num, 'precio_neto_final': num, 'rubro': 'str', 'marca': 'str'}}
        ]
    }}
    """
    
    archivo_part = types.Part.from_bytes(
        data=datos_para_enviar,
        mime_type=tipo_mime
    )
    
    response = client.models.generate_content(
        model=MODELO_IA,
        contents=[prompt, archivo_part],
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
        "hora": time.strftime("%H:%M:%S"),
        "monto_total": data.get('total_pagado', 0),
        "imagen_url": "whatsapp_doc"
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

def enviar_whatsapp(to_number, body_text):
    try:
        twilio_client.messages.create(
            from_='whatsapp:+14155238886', 
            body=body_text,
            to=f'whatsapp:{to_number}'
        )
    except Exception as e:
        print(f"Error enviando WA: {e}")

@app.route("/whatsapp", methods=['POST'])
def whatsapp_webhook():
    try:
        sender = request.form.get('From')
        media_url = request.form.get('MediaUrl0')
        content_type = request.form.get('MediaContentType0') 
        telefono_usuario = sender.replace("whatsapp:", "")
        
        print(f"1. Mensaje de {telefono_usuario}. Tipo: {content_type}")

        res = supabase.table('perfiles').select("id").eq('telefono', telefono_usuario).execute()
        if not res.data:
            enviar_whatsapp(telefono_usuario, "⛔ Regístrate primero en la web.")
            return str(MessagingResponse())
        
        user_id = res.data[0]['id']

        if media_url:
            print(f"2. Descargando {content_type}...")
            r = requests.get(media_url, auth=(TWILIO_SID, TWILIO_TOKEN))
            
            if r.status_code == 200:
                print("3. Analizando con IA...")
                datos = procesar_archivo_ia(r.content, content_type)
                
                if datos:
                    print("4. Guardando...")
                    cant = guardar_ticket(datos, user_id)
                    total = datos.get('total_pagado')
                    
                    if cant > 0:
                        tipo_doc = "PDF" if "pdf" in content_type else "Foto"
                        enviar_whatsapp(telefono_usuario, f"✅ *Guardado ({tipo_doc})*\n📍 {datos.get('supermercado')}\n💰 ${total}\n🛒 {cant} items")
                    else:
                        enviar_whatsapp(telefono_usuario, "⚠️ Archivo leído pero sin items.")
                else:
                    enviar_whatsapp(telefono_usuario, "❌ La IA no pudo leer el archivo.")
            else:
                enviar_whatsapp(telefono_usuario, "❌ Error descargando archivo.")
        else:
            enviar_whatsapp(telefono_usuario, "📸 Envíame una foto o un PDF.")

    except Exception as e:
        print(f"🔴 ERROR: {e}")
        traceback.print_exc()

    return str(MessagingResponse())

if __name__ == "__main__":
    app.run(debug=True, port=5000)