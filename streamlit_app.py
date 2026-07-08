import streamlit as st
import requests
import uuid

# Set up page config
st.set_page_config(
    page_title="IT Helpdesk Agent",
    page_icon="🤖",
    layout="centered"
)

# Backend FastAPI URL
BACKEND_URL = "http://localhost:8000"

st.title("🤖 IT Helpdesk Agent")
st.write("Welcome to the AcmeCorp L1 IT Support Chatbot.")

# Check health of the backend
try:
    health_resp = requests.get(f"{BACKEND_URL}/health", timeout=2)
    if health_resp.status_code == 200:
        health_data = health_resp.json()
        st.success(f"Connected to agent backend (AcmeCorp)")
    else:
        st.error("Agent backend returned an error. Make sure FastAPI server is running.")
except Exception:
    st.error("Could not connect to agent backend. Please start `python main.py` on port 8000 first.")

# Initialize session state for messages and session ID
if "messages" not in st.session_state:
    st.session_state.messages = []

if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())

# Display chat messages from history
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# Accept user input
if prompt := st.chat_input("What is your support query?"):
    # Display user message in chat message container
    with st.chat_message("user"):
        st.markdown(prompt)
    
    # Add user message to chat history
    st.session_state.messages.append({"role": "user", "content": prompt})

    # Call FastAPI backend
    with st.spinner("Agent is thinking..."):
        try:
            payload = {
                "message": prompt,
                "session_id": st.session_state.session_id
            }
            resp = requests.post(f"{BACKEND_URL}/chat", json=payload)
            if resp.status_code == 200:
                response_text = resp.json().get("response", "(no response)")
            else:
                response_text = f"Error: Backend returned status code {resp.status_code}. Detail: {resp.text}"
        except Exception as e:
            response_text = f"Failed to connect to agent: {str(e)}"

    # Display assistant response in chat message container
    with st.chat_message("assistant"):
        st.markdown(response_text)
    
    # Add assistant response to chat history
    st.session_state.messages.append({"role": "assistant", "content": response_text})
