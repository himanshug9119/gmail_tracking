import base64
import io
import os
import uuid
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request, send_file, url_for
from flask_cors import CORS
from pymongo import MongoClient

# --- Setup and Configuration ---
load_dotenv()
app = Flask(__name__)
CORS(app)

MONGO_URI = os.getenv('MONGO_URI')
if not MONGO_URI:
    raise ValueError("MONGO_URI is not set in the environment variables.")

client = MongoClient(MONGO_URI)
db = client.email_tracker

tracked_emails_collection = db.tracked_emails
open_events_collection = db.open_events

TRANSPARENT_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='
)

# --- NEW: Two-Step Tracking Logic ---

@app.route('/track')
def track_initial_request():
    """
    Step 1: The initial hit from the email client (proxy or real user).
    This endpoint's only job is to redirect to the final tracking URL.
    It does NOT log an open.
    """
    tracking_id = request.args.get('id')
    if not tracking_id:
        # Still return a pixel to prevent a broken image icon
        return send_file(io.BytesIO(TRANSPARENT_PNG), mimetype='image/png')

    # Generate a unique ID for this specific request to prevent replay issues.
    request_id = str(uuid.uuid4())

    # Create the URL for the second, definitive tracking step.
    # The `_external=True` is crucial to generate a full URL (e.g., http://yourdomain.com/track-final/...)
    final_url = url_for(
        'track_final_confirmation', 
        tracking_id=tracking_id, 
        request_id=request_id,
        _external=True
    )
    
    # We use a 307 Temporary Redirect. This tells the client "go here for this request".
    # A full browser will follow it, but many simple proxies will not.
    print(f"Initial hit for {tracking_id}. Redirecting to final URL.")
    return redirect(final_url, code=307)


@app.route('/track-final/<string:tracking_id>/<string:request_id>')
def track_final_confirmation(tracking_id, request_id):
    """
    Step 2: The confirmation hit. A request here is a HIGH CONFIDENCE signal
    that a real browser followed the redirect. THIS is what we log as an open.
    """
    ip_address = request.environ.get('HTTP_X_FORWARDED_FOR', request.remote_addr)
    user_agent = request.headers.get('User-Agent', '')
    now_iso = datetime.utcnow().isoformat() + 'Z'
    
    print(f"CONFIRMED open for {tracking_id} from {ip_address}. This is a real open.")

    event_record = {
        'uid': tracking_id,
        'ip': ip_address,
        'user_agent': user_agent,
        'opened_at': now_iso,
        'method': 'redirect_confirmed' # Add metadata on how we confirmed it
    }

    try:
        # Log the detailed event
        open_events_collection.insert_one(event_record)

        # Update the summary collection
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
    except Exception as e:
        print(f"Error logging final open to MongoDB: {e}")

    # Finally, return the transparent pixel to the browser.
    return send_file(io.BytesIO(TRANSPARENT_PNG), mimetype='image/png')


# --- API Endpoints for the Frontend (No changes needed) ---

@app.route('/api/new-id')
def generate_tracking_id():
    tracking_id = str(uuid.uuid4())
    return jsonify({'tracking_id': tracking_id})

@app.route('/api/opens')
def get_opens():
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

@app.route('/')
def home():
    return jsonify({'message': 'Email Open Tracker API is running'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)