import sys
from google import genai

# 1. Environment setup (based on demo.ipynb and colab_gemini_API.ipynb pattern)
if 'google.colab' in sys.modules:
    from google.colab import auth
    auth.authenticate_user()
    print("--- Environment: Google Colab (authenticated via auth) ---")
else:
    print("--- Environment: Codespace/Local (authenticated via gcloud ADC) ---")

# 2. Client initialization for project gen-lang-client-0464475716
# vertexai=True ensures use of professional GCP infrastructure
client = genai.Client(
    vertexai=True,
    project='gen-lang-client-0464475716',
    location='us-central1'
)

def list_and_test():
    try:
        # 3. List available models (following requested pattern)
        print("\nAvailable 'flash' family models in your project:")
        for m in client.models.list():
            if "flash" in m.name:
                print(f" - {m.name}")

        # 4. Content generation test
        # Using gemini-1.5-flash as the confirmed stable version
        print("\n--- Testing Content Generation ---")
        prompt = "As a professor, explain the importance of scientific AI in Civil Engineering."

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )

        print(f"Response:\n{response.text}")

    except Exception as e:
        print(f"\nError during execution: {e}")
        print("Tip: Verify that 'gcloud auth application-default login' completed successfully.")

if __name__ == "__main__":
    list_and_test()
