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
    img = Image.open(io.BytesIO(imagen_bytes))
    if img.width > 1024 or img.height > 1024:
        img.thumbnail((1024, 1024))
    return img

RUBROS_VALIDOS = "Almacén, Bebidas, Carnicería, Verdulería, Lácteos, Limpieza, Otros"

def procesar_ticket_ia(lista_imagenes_pil):
    # Armamos el contenido: Prompt + Todas las imágenes
    contenidos = []
    
    prompt = f"""
    Analiza estas imágenes que son PARTES DE UN MISMO TICKET (únelas lógicamente).
    1. Supermercado y Sucursal.
    2. Total Pagado.
    3. LISTA COMPLETA DE PRODUCTOS: Nombre, Cantidad, Precio Unitario.
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
    contenidos.append(prompt)
    
    # Agregamos cada imagen a la lista de contenidos
    for img in lista_imagenes_pil:
        contenidos.append(img)
    
    response = client.models.generate_content(
        model=MODELO_IA,
        contents=contenidos,
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
        "imagen_url": "whatsapp_multifoto"
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

# --- WEBHOOK ---
@app.route("/whatsapp", methods=['POST'])
def whatsapp_webhook():
    try:
        sender = request.form.get('From')
        num_media = int(request.form.get('NumMedia', 0)) # Cantidad de fotos
        telefono_usuario = sender.replace("whatsapp:", "")
        
        print(f"1. Mensaje de {telefono_usuario}. Fotos adjuntas: {num_media}")

        # Identificar Usuario
        res = supabase.table('perfiles').select("id").eq('telefono', telefono_usuario).execute()
        if not res.data:
            enviar_whatsapp(telefono_usuario, "⛔ Regístrate primero en la web.")
            return str(MessagingResponse())
        
        user_id = res.data[0]['id']

        if num_media > 0:
            print(f"2. Descargando {num_media} imágenes...")
            lista_imagenes = []
            
            # Iteramos por todas las fotos que mandó (MediaUrl0, MediaUrl1, etc)
            for i in range(num_media):
                url = request.form.get(f'MediaUrl{i}')
                r = requests.get(url, auth=(TWILIO_SID, TWILIO_TOKEN))
                if r.status_code == 200:
                    lista_imagenes.append(optimizar_imagen(r.content))
            
            if lista_imagenes:
                print("3. Enviando lote a IA...")
                datos = procesar_ticket_ia(lista_imagenes)
                
                if datos:
                    print("4. Guardando...")
                    cant = guardar_ticket(datos, user_id)
                    total = datos.get('total_pagado')
                    
                    if cant > 0:
                        enviar_whatsapp(telefono_usuario, f"✅ *Guardado (Multifoto)*\n📍 {datos.get('supermercado')}\n💰 ${total}\n🛒 {cant} items")
                    else:
                        enviar_whatsapp(telefono_usuario, "⚠️ Ticket vacío.")
                else:
                    enviar_whatsapp(telefono_usuario, "❌ La IA no pudo unir las fotos.")
            else:
                enviar_whatsapp(telefono_usuario, "❌ Error descargando imágenes.")
        else:
            enviar_whatsapp(telefono_usuario, "📸 Envíame fotos del ticket.")

    except Exception as e:
        print(f"🔴 ERROR: {e}")
        traceback.print_exc()

    return str(MessagingResponse())

if __name__ == "__main__":
    app.run(debug=True, port=5000)