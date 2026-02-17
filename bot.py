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
    """Achica la imagen si es muy grande, devuelve bytes"""
    try:
        img = Image.open(io.BytesIO(imagen_bytes))
        if img.width > 1024 or img.height > 1024:
            img.thumbnail((1024, 1024))
        # Convertir de vuelta a bytes para enviar a Gemini
        img_byte_arr = io.BytesIO()
        # Mantenemos el formato original o usamos JPEG por defecto
        fmt = img.format if img.format else 'JPEG'
        img.save(img_byte_arr, format=fmt)
        return img_byte_arr.getvalue()
    except:
        return imagen_bytes # Si falla, devuelve original

RUBROS_VALIDOS = """
- Almacén
- Bebidas s/Alcohol
- Bebidas c/Alcohol
- Carnicería
- Pescadería
- Frutas y Verduras
- Lácteos
- Quesos y Fiambres
- Panadería y Galletitas
- Golosinas
- Congelados y Helados
- Comida Elaborada / Rotisería
- Limpieza
- Perfumería e Higiene
- Farmacia
- Bebés y Maternidad
- Mascotas
- Electro y Tecnología
- Juguetería
- Ropa y Calzado
- Librería
- Hogar, Muebles y Bazar
- Ferretería y Herramientas
- Automotor
- Otros
"""

def procesar_lote_ia(lista_archivos):
    """
    Recibe una lista de diccionarios: [{'bytes': b'...', 'mime': 'image/jpeg'}, ...]
    """
    contenidos = []
    
    prompt = f"""
    Analiza estos archivos (Imágenes o PDF) que son UN SOLO TICKET/FACTURA.
    
    MISIÓN: Extraer datos y CÓDIGO DE BARRAS (EAN/GTIN) si existe.
    - EAN: 13 dígitos (ej: 779...), campo "codigo_barras".
    
    1. Comercio y Sucursal.
    2. Total Pagado.
    3. LISTA DE PRODUCTOS: Nombre, Cantidad, Precio Unitario, Rubro, Código.
    
    Rubros permitidos: {RUBROS_VALIDOS}.
    Si es remedio -> "Farmacia".
    
    JSON: {{
        'supermercado': 'str', 
        'total_pagado': num, 
        'fecha': 'YYYY-MM-DD', 
        'items': [
            {{'nombre': 'str', 'codigo_barras': 'str', 'cantidad': num, 'precio_neto_final': num, 'rubro': 'str', 'marca': 'str'}}
        ]
    }}
    """
    contenidos.append(prompt)
    
    # Agregamos cada archivo como un Part de Gemini
    for archivo in lista_archivos:
        part = types.Part.from_bytes(
            data=archivo['bytes'],
            mime_type=archivo['mime']
        )
        contenidos.append(part)
    
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

    # Upsert Lógico
    fecha_limpia = limpiar_fecha(data.get('fecha'))
    monto_limpio = limpiar_numero(data.get('total_pagado'))
    
    ticket_existente = supabase.table('tickets').select('id')\
        .eq('user_id', user_id)\
        .eq('supermercado_id', super_id)\
        .eq('fecha', fecha_limpia)\
        .eq('monto_total', monto_limpio)\
        .execute()
    
    status_msg = ""
    if ticket_existente.data:
        id_viejo = ticket_existente.data[0]['id']
        supabase.table('tickets').delete().eq('id', id_viejo).execute()
        status_msg = "(Actualizado ♻️)"

    ticket_data = {
        "user_id": user_id,
        "supermercado_id": super_id,
        "fecha": fecha_limpia,
        "hora": time.strftime("%H:%M:%S"),
        "monto_total": monto_limpio,
        "imagen_url": "whatsapp_bot_multi"
    }
    res_ticket = supabase.table('tickets').insert(ticket_data).execute()
    ticket_id = res_ticket.data[0]['id']

    items_db = []
    for item in data.get('items', []):
        items_db.append({
            "ticket_id": ticket_id,
            "nombre_producto": item.get('nombre', 'Item'),
            "cantidad": limpiar_numero(item.get('cantidad', 1)),
            "precio_neto_unitario": limpiar_numero(item.get('precio_neto_final', 0)),
            "unidad_medida": "Un",
            "rubro": item.get('rubro'),
            "marca": item.get('marca'),
            "codigo_barras": item.get('codigo_barras')
        })
    
    if items_db:
        supabase.table('items_compra').insert(items_db).execute()
        return len(items_db), status_msg
    return 0, ""

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
        num_media = int(request.form.get('NumMedia', 0)) 
        telefono_usuario = sender.replace("whatsapp:", "")
        
        print(f"1. Mensaje de {telefono_usuario}. Archivos: {num_media}")

        res = supabase.table('perfiles').select("id").eq('telefono', telefono_usuario).execute()
        if not res.data:
            enviar_whatsapp(telefono_usuario, "⛔ Regístrate primero en la web.")
            return str(MessagingResponse())
        
        user_id = res.data[0]['id']

        if num_media > 0:
            print(f"2. Descargando {num_media} archivos...")
            archivos_procesados = []
            
            for i in range(num_media):
                url = request.form.get(f'MediaUrl{i}')
                content_type = request.form.get(f'MediaContentType{i}')
                
                r = requests.get(url, auth=(TWILIO_SID, TWILIO_TOKEN))
                if r.status_code == 200:
                    datos_archivo = r.content
                    
                    # SI ES IMAGEN -> OPTIMIZAR
                    if "image" in content_type:
                        datos_archivo = optimizar_imagen(datos_archivo)
                    
                    # SI ES PDF -> PASAR DIRECTO (Gemini lo lee nativo)
                    
                    archivos_procesados.append({
                        'bytes': datos_archivo,
                        'mime': content_type
                    })
            
            if archivos_procesados:
                print("3. Enviando a IA...")
                datos = procesar_lote_ia(archivos_procesados)
                
                if datos:
                    print("4. Guardando...")
                    cant, status = guardar_ticket(datos, user_id)
                    total = datos.get('total_pagado')
                    
                    if cant > 0:
                        enviar_whatsapp(telefono_usuario, f"✅ *Guardado {status}*\n📍 {datos.get('supermercado')}\n💰 ${total}\n🛒 {cant} items")
                    else:
                        enviar_whatsapp(telefono_usuario, "⚠️ Archivo leído pero sin items.")
                else:
                    enviar_whatsapp(telefono_usuario, "❌ La IA no pudo leer el archivo.")
            else:
                enviar_whatsapp(telefono_usuario, "❌ Error descargando archivos.")
        else:
            enviar_whatsapp(telefono_usuario, "📸 Envíame foto o PDF.")

    except Exception as e:
        print(f"🔴 ERROR: {e}")
        traceback.print_exc()

    return str(MessagingResponse())

if __name__ == "__main__":
    app.run(debug=True, port=5000)