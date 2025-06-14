import base64
import io
import os
import uuid
from datetime import datetime

import requests  # <-- We need this for the API call
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request, send_file, url_for
from flask_cors import CORS
from pymongo import MongoClient

# --- Setup and Configuration ---
load_dotenv()
app = Flask(__name__)
CORS(app)

MONGO_URI = os.getenv('MONGO_URI')
ABSTRACT_API_KEY = os.getenv('ABSTRACT_API_KEY')

if not MONGO_URI or not ABSTRACT_API_KEY:
    raise ValueError("MONGO_URI and ABSTRACT_API_KEY must be set in the environment.")

client = MongoClient(MONGO_URI)
db = client.email_tracker

tracked_emails_collection = db.tracked_emails
open_events_collection = db.open_events

TRANSPARENT_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='
)

# --- NEW: Helper function for IP Geolocation Enrichment ---

def get_ip_info(ip_address: str) -> dict:
    """
    Fetches detailed geolocation information for a given IP address using AbstractAPI.
    Returns a dictionary with the info or an empty dict on failure.
    """
    # Don't make API calls for private/local IPs
    if ip_address.startswith(('127.0.0.1', '192.168.', '10.', '172.')):
        return {'note': 'Private/Local IP Address'}

    try:
        url = f"https://ipgeolocation.abstractapi.com/v1/?api_key={ABSTRACT_API_KEY}&ip_address={ip_address}"
        response = requests.get(url, timeout=5) # Added a timeout for safety
        if response.status_code == 200:
            data = response.json()
            # We can select which fields we want to store
            return {
                'city': data.get('city'),
                'region': data.get('region'),
                'country': data.get('country'),
                'country_code': data.get('country_code'),
                'continent': data.get('continent'),
                'latitude': data.get('latitude'),
                'longitude': data.get('longitude'),
                'isp': data.get('connection', {}).get('isp_name'),
                'connection_type': data.get('connection', {}).get('connection_type'),
            }
        else:
            print(f"IP API request failed with status {response.status_code}.")
            return {'error': f"API failed with status {response.status_code}"}

    except requests.exceptions.RequestException as e:
        print(f"Error calling IP reputation API for {ip_address}: {e}")
        return {'error': 'API request failed'}

    return {} # Return empty dict on any other failure


# --- Two-Step Tracking Logic with Data Enrichment ---

@app.route('/track')
def track_initial_request():
    """Step 1: The initial hit. Its only job is to redirect."""
    tracking_id = request.args.get('id')
    if not tracking_id:
        return send_file(io.BytesIO(TRANSPARENT_PNG), mimetype='image/png')
    
    request_id = str(uuid.uuid4())
    final_url = url_for(
        'track_final_confirmation', 
        tracking_id=tracking_id, 
        request_id=request_id,
        _external=True
    )
    return redirect(final_url, code=307)


@app.route('/track-final/<string:tracking_id>/<string:request_id>')
def track_final_confirmation(tracking_id, request_id):
    """
    Step 2: The confirmation hit where we apply our logic and enrich the data.
    """
    user_agent = request.headers.get('User-Agent', '')
    # Get the real client IP, even behind proxies like Render's
    ip_address = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
    now_iso = datetime.utcnow().isoformat() + 'Z'
    
    is_google_proxy = 'GoogleImageProxy' in user_agent

    # Base record for all events
    event_record = {
        'uid': tracking_id,
        'ip': ip_address,
        'user_agent': user_agent,
        'opened_at': now_iso,
        'is_real_open': is_google_proxy
    }
    
    try:
        # --- DATA ENRICHMENT STEP ---
        if is_google_proxy:
            print(f"CONFIRMED GMAIL OPEN for {tracking_id}. Fetching IP info for {ip_address}...")
            # Get the IP details of the Google Proxy server that made the request.
            # This tells you where in the world Google processed the open.
            ip_info = get_ip_info(ip_address)
            event_record['geo_info'] = ip_info # Add the rich data to our event record
            
            # Update summary collection
            tracked_emails_collection.update_one(
                {'_id': tracking_id},
                {
                    '$inc': {'open_count': 1},
                    '$set': {'last_opened_at': now_iso},
                    '$setOnInsert': {
                        '_id': tracking_id,
                        'first_opened_at': now_iso,
                        'created_at': now_iso,
                    }
                },
                upsert=True
            )
        else:
            print(f"Ignoring browser hit for {tracking_id} (likely sender's compose window)")

        # Log the event, now with geo_info if it was a real open
        open_events_collection.insert_one(event_record)

    except Exception as e:
        print(f"Error logging final open to MongoDB: {e}")

    return send_file(io.BytesIO(TRANSPARENT_PNG), mimetype='image/png')


# --- API Endpoints ---

@app.route('/api/new-id')
def generate_tracking_id():
    return jsonify({'tracking_id': str(uuid.uuid4())})

@app.route('/api/opens')
def get_opens():
    """
    This endpoint now returns the fully enriched data, including the `geo_info`
    object for real opens, which you can use in your frontend later.
    """
    tracking_id = request.args.get('id')
    query = {}
    if tracking_id:
        query = {'uid': tracking_id}
    try:
        opens = list(open_events_collection.find(query, {'_id': 0}).sort('opened_at', -1))
        return jsonify({'opens': opens})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/stats')
def get_stats():
    """
    This endpoint remains accurate, counting only the real opens.
    """
    try:
        pipeline = [{'$group': {'_id': None, 'total': {'$sum': '$open_count'}}}]
        aggregation_result = list(tracked_emails_collection.aggregate(pipeline))
        total_opens = aggregation_result[0]['total'] if aggregation_result else 0
        unique_ids = tracked_emails_collection.count_documents({})
        return jsonify({
            'total_opens': total_opens,
            'unique_tracking_ids': unique_ids
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# NEW: An endpoint to get enriched details for a single tracking ID
@app.route('/api/details/<string:tracking_id>')
def get_tracking_details(tracking_id):
    """
    Provides a summary and all real open events for a specific tracking ID.
    This is useful for a "details" page in your frontend.
    """
    try:
        summary = tracked_emails_collection.find_one({'_id': tracking_id}, {'_id': 0})
        if not summary:
            return jsonify({'error': 'Tracking ID not found'}), 404

        # Find all the "real" open events associated with this ID
        real_open_events = list(open_events_collection.find(
            {'uid': tracking_id, 'is_real_open': True}, 
            {'_id': 0}
        ).sort('opened_at', 1)) # Sort ascending for a timeline view

        summary['open_events'] = real_open_events

        return jsonify(summary)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/')
def home():
    return jsonify({
        'message': 'Email Open Tracker API is running',
        'endpoints': {
            'new_id': '/api/new-id',
            'opens': '/api/opens?id=<id>',
            'stats': '/api/stats',
            'details': '/api/details/<id>'
        }
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)