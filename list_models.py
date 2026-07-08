import os
from dotenv import load_dotenv
load_dotenv()

# We can try to list models using google-genai
try:
    from google import genai
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    print("=== Models via google-genai ===")
    for model in client.models.list():
        print(model.name)
except Exception as e:
    print(f"Error with google-genai: {e}")

try:
    import google.generativeai as generativeai
    generativeai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
    print("\n=== Models via google.generativeai ===")
    for m in generativeai.list_models():
        if 'generateContent' in m.supported_generation_methods:
            print(m.name)
except Exception as e:
    print(f"Error with google.generativeai: {e}")
