import os
import logging
import requests
import redis
from flask import Flask, request, jsonify, render_template, Response, stream_with_context, session, flash, request, redirect, url_for, send_file
from functools import wraps
from dotenv import load_dotenv
from openai import AzureOpenAI
import json
from datetime import datetime, timedelta
import re
from doc import Documents
import sqlite3


load_dotenv()


app = Flask(__name__)

app.secret_key = 'fs78sf7s8d6v7sdy7sdbds7v'


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# Initialize Redis client
redis_host = os.getenv('REDIS_HOST', 'localhost')
redis_port = os.getenv('REDIS_PORT', 6379)
redis_client = redis.Redis(host=redis_host, port=int(redis_port), db=0)



# Azure credentials and endpoints
AZURE_SEARCH_ENDPOINT = os.getenv('AZURE_SEARCH_ENDPOINT', 'https://ai-search-ehcd.search.windows.net')
AZURE_SEARCH_KEY = os.getenv('AZURE_SEARCH_API_KEY', 'put your Azure AI Search admin key here')
AZURE_OPENAI_ENDPOINT = os.getenv('ENDPOINT_URL', 'https://ai-ehcd.openai.azure.com')
AZURE_OPENAI_KEY = os.getenv('AZURE_OPENAI_API_KEY', 'REPLACE_WITH_YOUR_KEY_VALUE_HERE')
AZURE_OPENAI_DEPLOYMENT = os.getenv('DEPLOYMENT_NAME', 'gpt-4o')
SEARCH_INDEX_NAME = os.getenv('SEARCH_INDEX_NAME', 'rag-1')

# Initialize Azure OpenAI client
logger.info("Initializing Azure OpenAI client")
client = AzureOpenAI(
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
    api_key=AZURE_OPENAI_KEY,
    api_version="2024-12-01-preview",
)
logger.info("Azure OpenAI client initialized successfully")




documents = Documents()
documents.save_local_files_to_db()



# Function to get conversation history from Redis
def get_conversation_history(user_id):
    history = redis_client.get(f"user_{user_id}_history")
    if history:
        return json.loads(history)
    return []

# Function to save conversation history to Redis
def save_conversation_history(user_id, history):
    redis_client.set(f"user_{user_id}_history", json.dumps(history), ex=3600)  

# Query Azure Cognitive Search to include rich media content
def query_azure_search(query):
   
    search_url = f"{AZURE_SEARCH_ENDPOINT}/indexes/{SEARCH_INDEX_NAME}/docs/search?api-version=2023-07-01-Preview"
    headers = {
        'Content-Type': 'application/json',
        'api-key': AZURE_SEARCH_KEY
    }
    payload = {
        'search': query,
        'queryType': 'semantic',
        'top': 10,  
        'select': 'chunk,project_name,chunk_id',
        'count': 'true'  
    }
    
    response = requests.post(search_url, headers=headers, json=payload)
   
    if response.status_code == 200:
        logger.info("Azure Cognitive Search query successful")
        return response.json().get('value', [])
    else:
        logger.error(f"Error querying Azure Search: {response.status_code} - {response.text}")
        return []


# --------------------------------------------- End of Azure Query ----------------------------------------------------------------------




# ----------------------------------------------- GPT functionaility and Responses-------------------------------------------------------

def trim_history(conversation_history, max_entries=5):
    return conversation_history[-max_entries:]


# Azure OpenAI response generator - with hsitory using redis
def generate_gpt_response(context, query, conversation_history):

    trimmed_history = trim_history(conversation_history)
    history_prompt = "\n".join(
        [f"{entry['role']}: {entry['content']}" for entry in trimmed_history]
    )



    history_prompt += f"\nuser: {query}"

    chat_prompt = [
    {
        "role": "system",
        "content": f"""
               You are an expert advisor for the Education, Human Development, and Community Development Council (EHCD), focusing on education project insights, strategic planning, and community well-being. The knowledge base is: "{context}" and handle greetings appropriately, ensuring that the conversation flows naturally. Respond using only information from the provided knowledge base. Use the following conversation history to maintain context: {history_prompt}. 
               Never forget History Conversation:{history_prompt}, you have to response following the context provided.
               Title of the document is in the knowledge base
                
        User Objective:
        The user seeks insights on ongoing or planned education projects, their budgets, strategies, timelines, or policy implications. Your task is to extract relevant information from the knowledge base and provide a clear, human-friendly explanation. Focus on delivering answers that are:

            - Summarize without missing any relevant detail, necessary for the user.
            - To the Point: Answer directly with what is specified in the knowledge base.
            - Structured: Use bullet points, numbered lists, or tables as appropriate for clarity.
            - After providing an overview, ask follow-up questions for example:
                - "Would you like more details on any specific aspect?"
                - "Is there a particular area you’d like to explore further?"
                - "Should I elaborate on this project's budget or scope?"
                - "Would you like examples or comparisons with other EHCD initiatives?"

            Ensure that responses are well-structured but offer to provide more details in a conversational manner, allowing the user to guide the depth of the discussion.

        Instructions:

            Search the Knowledge Base:
                - Do not invent or create information by yourself if not provided in the context or knowledge base.
                - Understand the user query and the context provided, if the information is not valid for user query , just reply,  i dont't have such information regarding your query.
                - Identify the most relevant document(s) based on the user's question.
                - Always respond in the **same language** as the user's question (e.g., if asked in Arabic, respond fully in Arabic).
                - Extract only the information directly related to the user’s query.
                - If the knowledge base does not contain the requested information, respond with: "The requested information isn't directly available in the provided documents."
                - you can respond to the following question, if asked for more information, summarize the answer or engage in further dialogue using history Chat -> "History Conversation" to understand the query better.
                - Make the conversation feel human-like by engaging in back-and-forth interactions when necessary (e.g., ask clarifying questions if the user requests a table or detailed breakdown).
                - if user ask about image, provide the flowchart and answer respectively
                - Do not mention 
                

            Answer Structuring:
                  Use proper HTML for structuring and Styling your response: (Aesthetics are must)
                    - Ensure all text formatting uses only HTML tags (e.g., `<h3>`, `<ul>`, `<strong>`, `<br>`, etc.) for headings, lists, emphasis, and line breaks. Avoid `\n` for spacing.
                    - Headings
                        Do Not Use: Markdown symbols like #, ##, etc.
                        Use: HTML heading tags <h1> to <h6>.
                    Example:

                    <h1>Main Title</h1>
                    <h2>Subheading</h2>
                    <h3>Section Heading</h3>
                    Create Lists Using Proper HTML Tags

                    Unordered Lists (Bullet Points)
                        Do Not Use: Dash (-) or asterisk (*) symbols.
                        Use: <ul> for the list container and <li> for each list item.

                        Example:

                        <ul>
                        <li>First item</li>
                        <li>Second item</li>
                        <li>Third item</li>
                        </ul>

                Ordered Lists (Numbered Lists)

                    Do Not Use: Numbers followed by periods (e.g., 1., 2.) in plain text.
                    Use: <ol> for the list container and <li> for each list item.
                    Example:

                    <ol>
                    <li>First step</li>
                    <li>Second step</li>
                    <li>Third step</li>
                    </ol>


                    - Wrap any table content in <table><tr><td>...</td></tr></table> tags for tabular data.
                    - If User Ask for "Table" format the answer in table , If ask "flowchart" you have to provide the best flow chart with proper styling using html and css.
                    - Do not include HTML tags that are not properly closed.
                    - Ensure that the HTML content is easy to read and well-formatted for a better user experience.
                    

            Conversational Clarity:
                - If the user asks for more details or specifics (e.g., "Can you make a table for this?"), follow up with a question like "Sure, what data would you like in the table?" or "Which details should be included in the table?".
                - For general questions, summarize and then ask, "Would you like more details on any specific point?" to keep the interaction dynamic.
                - Aim for a tone that feels like a natural conversation rather than a strict Q&A format.

            Clarity & Structure:
                - Ensure that all responses are well-structured, easy to read, and follow a logical flow.
                - Avoid using any unnecessary names or content not related to the provided context.
            You have to remember:
                - Avoid Code Markers:" Do not use ''',** backticks (`), or any code block delimiters (like '''html or backticks)".

                    
            Example Query Handling:
                User: "What projects are currently being run by EHCD to improve school attendance?"
                Chatbot Response:

                <h3>EHCD Projects for Improving School Attendance</h3>
                <ul>
                  <li><strong>Smart Attendance Monitoring System:</strong> Uses biometric and RFID tech to track attendance.</li>
                  <li><strong>Parent-Engagement Workshops:</strong> Monthly sessions to engage parents on student participation.</li>
                  <li><strong>Transportation Access Program:</strong> Providing buses for remote areas to ensure daily school access.</li>
                </ul>

                Would you like a table with timelines and allocated budgets for each initiative?

                """
        },
        {"role": "user", "content": query},
    ]

    try:
        response_stream = client.chat.completions.create(
            model=AZURE_OPENAI_DEPLOYMENT,
            messages=chat_prompt,
            max_tokens=1000,
            temperature=0.7,
            top_p=0.95,
            frequency_penalty=0.2,
            presence_penalty=0,
            stop=None,
            stream=True
            # stream_options={"include_usage": True}
        )



        # for chunk in response_stream:
        #     # final usage arrives here (one time, at the end)
        #     if getattr(chunk, "usage", None):
        #         print(f"Total tokens used: {int(getattr(chunk.usage, 'total_tokens', 0) or 0)}")
        #         continue
        
        for chunk in response_stream:
            if chunk.choices and len(chunk.choices) > 0:
                content = chunk.choices[0].delta.content
                if content:
                    yield content

       

    except Exception as e:
        logger.error(f"Error generating GPT response: {e}")
        return "An error occurred while processing your request."


# ------------------ Selecting Relevant Chunks --------------------------------------------------
def select_relevant_chunks(search_results, max_chunks=10, relevance_threshold=2.0):
    # Filter out chunks below the relevance threshold
    filtered_results = [
        doc for doc in search_results if doc.get('@search.score', 0) >= relevance_threshold
    ]
    
    
    sorted_results = sorted(filtered_results, key=lambda x: x.get('@search.score', 0), reverse=True)
    top_chunks = [doc.get('chunk', '') for doc in sorted_results[:max_chunks]]


   
    
    return ' '.join(top_chunks)







#------------------------------------------------------ API route for handling user queriees and generating responses using GPT + Azure Cognitive Search

@app.route('/api/query', methods=['POST'])
def handle_query():
    query = request.json.get('query', '')

    # Initialize User sessions, if not created
    if 'user_id' not in session:
        session['user_id'] = os.urandom(16).hex()
    
    user_id = session['user_id']

    # Retrieve conversation history using user_id
    conversation_history = get_conversation_history(user_id)
    conversation_history.append({"role": "user", "content": query})

    # Retrieve relevant documents from Azure Cognitive Search
    search_results = query_azure_search(query)
    
    context = select_relevant_chunks(search_results, max_chunks=10, relevance_threshold=2.0)
    logger.info("Context:\n%s", context)


    try:
        gpt_response_generator = generate_gpt_response(context, query, conversation_history)

        def generate():
            assistant_response = ''
            for chunk in gpt_response_generator:
                assistant_response += chunk
                # Strip HTML tags for incremental display
                plain_text_chunk = re.sub(r'<[^>]*>', '', chunk)
                yield plain_text_chunk

            # Append full HTML response after all chunks are received
            conversation_history.append({"role": "assistant", "content": assistant_response})
            save_conversation_history(user_id, conversation_history)

            if assistant_response:
                yield f"<replace>{assistant_response}</replace>"

        response = Response(stream_with_context(generate()), content_type='text/html', headers={'Content-Encoding': 'chunked'})
        
        return response

    except Exception as e:
        logger.error(f"Error while streaming GPT response: {e}")
        return jsonify({'error': 'An error occurred while processing your request.'}),


# ---------------------------------------------Start Login Sessions -------------------------------------------------

credentials = {
    "hypernym1": "hyper@chatbot",
    "hypernym2": "hyper@chatbot"    
}



app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=1)

# Initialize SQLite database
def init_db():
    conn = sqlite3.connect('sessions.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS active_sessions (username TEXT PRIMARY KEY, last_active TIMESTAMP)''')
    conn.commit()
    conn.close()

# Function to add a user session to the database
def add_session(username):
    conn = sqlite3.connect('sessions.db')
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO active_sessions (username, last_active) VALUES (?, ?)", 
                   (username, datetime.now()))
    conn.commit()
    conn.close()

# Function to remove a user session from the database
def remove_session(username):
    conn = sqlite3.connect('sessions.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM active_sessions WHERE username = ?", (username,))
    conn.commit()
    conn.close()

# Function to count active sessions
def count_active_sessions():
    cleanup_expired_sessions()  
    conn = sqlite3.connect('sessions.db')
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM active_sessions")
    count = cursor.fetchone()[0]
    conn.close()
    return count

# Function to check if a user is already logged in
def is_user_logged_in(username):
    cleanup_expired_sessions()  
    conn = sqlite3.connect('sessions.db')
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM active_sessions WHERE username = ?", (username,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

# Cleanup function to remove expired sessions
def cleanup_expired_sessions():
    expiration_time = datetime.now() - app.config['PERMANENT_SESSION_LIFETIME']
    conn = sqlite3.connect('sessions.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM active_sessions WHERE last_active < ?", (expiration_time,))
    conn.commit()
    conn.close()

# Decorator to require login for specific routes
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session or not is_user_logged_in(session['username']):
            flash("Please log in to access this page.")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/', methods=['GET', 'POST'])
def login():
    init_db()  
    if count_active_sessions() >= 2:
        flash('Maximum number of users are currently logged in. Please wait until someone logs out.')
        return redirect(url_for('login'))

    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        # Validate credentials
        if credentials.get(username) == password:
            if is_user_logged_in(username):
                flash('This user is already logged in from another session.')
                return redirect(url_for('login'))
            else:
                session['username'] = username  
                session.permanent = True  
                add_session(username)  
                return redirect(url_for('index'))
        else:
            flash('Invalid credentials. Please try again.')
            return redirect(url_for('login'))

    return render_template('login.html')



@app.route('/logout')
@login_required
def logout():
    username = session.pop('username', None)
    if username:
        remove_session(username)  
    flash('You have been logged out.')
    return redirect(url_for('login'))

# ----------------------------------------------- End of Login Sessions -------------------------------------------------

@app.route('/documents')
@login_required
def list_documents():
    if session.get("username") == "hypernym1":
        document_list = documents.fetch_documents()
        return render_template('documents.html', documents=document_list)
    else:
        return "Unauthorized", 401

# Route to download a specific document by ID
@app.route('/download/<int:document_id>')
def download_document(document_id):
    document = documents.get_document_path(document_id)
    if document:
        document_name, file_path = document
        return send_file(file_path, as_attachment=True)
    return "Document not found", 404

# ------------------------------------------------------ End of Document Management -----------------------------------------------

@app.route('/home')
@login_required
def index():
    return render_template('index.html')



if __name__ == '__main__':
    logger.info("Starting Chatbot web application")
    app.run(host='0.0.0.0', port=8080, debug=True)
