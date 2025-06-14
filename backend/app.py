import base64
import io
import os
import uuid
from datetime import datetime

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request, send_file, url_for
from flask_cors import CORS
from pymongo import MongoClient

# --- 1. SETUP AND CONFIGURATION ---
load_dotenv()
app = Flask(__name__)
CORS(app)

# --- 2. DATABASE AND CONSTANTS ---
MONGO_URI = os.getenv('MONGO_URI')
ABSTRACT_API_KEY = os.getenv('ABSTRACT_API_KEY')

if not MONGO_URI or not ABSTRACT_API_KEY:
    raise ValueError("MONGO_URI and ABSTRACT_API_KEY must be set in the environment.")

client = MongoClient(MONGO_URI)
db = client.email_tracker

tracked_emails_collection = db.tracked_emails
open_events_collection = db.open_events
clicks_collection = db.clicks # Ensure this collection is defined

TRANSPARENT_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='
)

# --- 3. HELPER FUNCTIONS ---

def get_ip_info(ip_address: str) -> dict:
    """Fetches detailed geolocation information for a given IP address."""
    if ip_address.startswith(('127.0.0.1', '192.168.', '10.', '172.')):
        return {'note': 'Private/Local IP Address'}
    try:
        url = f"https://ipgeolocation.abstractapi.com/v1/?api_key={ABSTRACT_API_KEY}&ip_address={ip_address}"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            return {
                'city': data.get('city'), 'region': data.get('region'),
                'country': data.get('country'), 'country_code': data.get('country_code'),
                'continent': data.get('continent'), 'latitude': data.get('latitude'),
                'longitude': data.get('longitude'), 'isp': data.get('connection', {}).get('isp_name'),
                'connection_type': data.get('connection', {}).get('connection_type'),
            }
        else:
            print(f"IP API request failed with status {response.status_code}.")
            return {'error': f"API failed with status {response.status_code}"}
    except requests.exceptions.RequestException as e:
        print(f"Error calling IP reputation API for {ip_address}: {e}")
        return {'error': 'API request failed'}
    return {}

# --- 4. CORE TRACKING ROUTES ---

@app.route('/track')
def track_initial_request():
    """Step 1: The initial hit. Its only job is to redirect."""
    tracking_id = request.args.get('id')
    if not tracking_id:
        return send_file(io.BytesIO(TRANSPARENT_PNG), mimetype='image/png')
    
    request_id = str(uuid.uuid4())
    final_url = url_for('track_final_confirmation', tracking_id=tracking_id, request_id=request_id, _external=True)
    return redirect(final_url, code=307)


@app.route('/track-final/<string:tracking_id>/<string:request_id>')
def track_final_confirmation(tracking_id, request_id):
    """Step 2: The confirmation hit where we apply our logic and enrich the data."""
    user_agent = request.headers.get('User-Agent', '')
    ip_address = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
    now_iso = datetime.utcnow().isoformat() + 'Z'
    is_google_proxy = 'GoogleImageProxy' in user_agent

    event_record = {'uid': tracking_id, 'ip': ip_address, 'user_agent': user_agent, 'opened_at': now_iso, 'is_real_open': is_google_proxy}
    
    try:
        if is_google_proxy:
            print(f"CONFIRMED GMAIL OPEN for {tracking_id}. Fetching IP info for {ip_address}...")
            ip_info = get_ip_info(ip_address)
            event_record['geo_info'] = ip_info
            
            tracked_emails_collection.update_one(
                {'_id': tracking_id},
                {'$inc': {'open_count': 1}, '$set': {'last_opened_at': now_iso},
                 '$setOnInsert': {'_id': tracking_id, 'first_opened_at': now_iso, 'created_at': now_iso}},
                upsert=True)
        else:
            print(f"Ignoring browser hit for {tracking_id} (likely sender's compose window)")
        
        open_events_collection.insert_one(event_record)
    except Exception as e:
        print(f"Error logging final open to MongoDB: {e}")

    return send_file(io.BytesIO(TRANSPARENT_PNG), mimetype='image/png')


@app.route('/click')
def track_click():
    """Tracks a link click and redirects the user to the final destination."""
    tracking_id = request.args.get('uid')
    destination_url = request.args.get('url')

    if not tracking_id or not destination_url:
        return "Error: Missing tracking ID or destination URL.", 400

    if not destination_url.startswith('http://') and not destination_url.startswith('https://'):
        return "Error: Invalid destination URL format.", 400

    ip_address = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
    user_agent = request.headers.get('User-Agent', '')
    now_iso = datetime.utcnow().isoformat() + 'Z'
    geo_info = get_ip_info(ip_address)

    click_record = {
        'uid': tracking_id,
        'destination_url': destination_url,
        'ip': ip_address, 'user_agent': user_agent,
        'clicked_at': now_iso, 'geo_info': geo_info,
    }

    try:
        clicks_collection.insert_one(click_record)
        # Add a new field to the summary to track clicks
        tracked_emails_collection.update_one(
            {'_id': tracking_id},
            {'$inc': {'click_count': 1}}
        )
        print(f"Tracked CLICK for {tracking_id} to {destination_url}")
    except Exception as e:
        print(f"Error logging click: {e}")

    return redirect(destination_url, code=307)


# --- 5. API ROUTES FOR FRONTEND ---

@app.route('/api/opens')
def get_opens():
    """Gets all open events, optionally filtered by tracking ID."""
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
    """Gets high-level statistics about all tracked emails."""
    try:
        pipeline = [{'$group': {'_id': None, 'total_opens': {'$sum': '$open_count'}, 'total_clicks': {'$sum': '$click_count'}}}]
        aggregation_result = list(tracked_emails_collection.aggregate(pipeline))
        
        total_opens = 0
        total_clicks = 0
        if aggregation_result:
            total_opens = aggregation_result[0].get('total_opens', 0)
            total_clicks = aggregation_result[0].get('total_clicks', 0)

        unique_ids = tracked_emails_collection.count_documents({})
        return jsonify({
            'total_opens': total_opens,
            'total_clicks': total_clicks,
            'unique_tracking_ids': unique_ids
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/details/<string:tracking_id>')
def get_tracking_details():
    """Provides a detailed summary for a specific tracking ID, including clicks."""
    try:
        summary = tracked_emails_collection.find_one({'_id': tracking_id}, {'_id': 0})
        if not summary:
            return jsonify({'error': 'Tracking ID not found'}), 404

        real_open_events = list(open_events_collection.find({'uid': tracking_id, 'is_real_open': True}, {'_id': 0}).sort('opened_at', 1))
        click_events = list(clicks_collection.find({'uid': tracking_id}, {'_id': 0}).sort('clicked_at', 1))

        summary['open_events'] = real_open_events
        summary['click_events'] = click_events

        return jsonify(summary)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# --- 6. ROOT/HOME ROUTE ---

@app.route('/')
def home():
    """Simple API info endpoint for the root URL."""
    return jsonify({
        'message': 'Email Open Tracker API is running',
        'status': 'healthy',
        'endpoints': {
            'track_pixel': '/track?id=<id>',
            'track_click': '/click?uid=<id>&url=<encoded_url>',
            'get_opens': '/api/opens?id=<id>',
            'get_stats': '/api/stats',
            'get_details': '/api/details/<id>'
        }
    })


# --- 7. SCRIPT EXECUTION ---

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)