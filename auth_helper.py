import streamlit as st
import google_auth_oauthlib.flow
import google.auth.transport.requests
import google.oauth2.credentials
import json
import os

# Scopes required for Gemini
SCOPES = ['https://www.googleapis.com/auth/generative-language.retriever', 'https://www.googleapis.com/auth/cloud-platform']

def authenticate_user():
    """
    Handles Google OAuth2 Flow for "Sign in with Google".
    Returns credentials object if successful, None otherwise.
    """
    st.subheader("🔐 Google Login")

    # 1. Client Secrets Management
    # We need the client_secret.json content. We can ask user to paste it.
    
    if "oauth_secrets" not in st.session_state:
        st.session_state.oauth_secrets = None

    if not st.session_state.oauth_secrets:
        with st.expander("⚙️ Setup Google Login (One-time)", expanded=True):
            st.markdown("""
            To log in with your Google Account, you need an **OAuth Client ID** from Google Cloud.
            
            1. Go to [Google Cloud Console](https://console.cloud.google.com/).
            2. Create a project and enable the **Generative Language API**.
            3. Go to **APIs & Services > Credentials**.
            4. Create Credentials > **OAuth Client ID**.
            5. Select application type: **Desktop App** (easiest for this setup).
            6. Download the JSON file and paste the contents below.
            """)
            secrets_input = st.text_area("Paste 'client_secret.json' content here:", height=150)
            if secrets_input:
                try:
                    loaded = json.loads(secrets_input)
                    # Verify basic structure
                    if 'installed' in loaded or 'web' in loaded:
                        st.session_state.oauth_secrets = loaded
                        st.success("Secrets loaded! You can now log in.")
                        st.rerun()
                    else:
                        st.error("Invalid JSON. Look for 'installed' or 'web' keys.")
                except:
                    st.error("Invalid JSON format.")
        return None

    # 2. Authorization Flow
    if "google_creds" not in st.session_state:
        st.session_state.google_creds = None

    # Check if we have valid cached creds
    if st.session_state.google_creds:
        creds = st.session_state.google_creds
        if creds.valid:
            return creds
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(google.auth.transport.requests.Request())
                return creds
            except:
                st.session_state.google_creds = None
    
    # Start new flow
    if st.session_state.oauth_secrets:
        try:
            # Use 'installed' (Desktop) or 'web' config
            # We treat it as installed so we can just do the manual copy-paste flow which is robust for pure Streamlit
            flow = google_auth_oauthlib.flow.InstalledAppFlow.from_client_config(
                st.session_state.oauth_secrets, SCOPES)
            
            # Since we are in a web app, we cannot launch a local server browser pop-up on the SERVER side easily.
            # We uses the manual "Copy link / Paste code" method (OOB).
            
            # Create redirect_uri for manual flow
            flow.redirect_uri = 'urn:ietf:wg:oauth:2.0:oob'
            
            auth_url, _ = flow.authorization_url(prompt='consent')
            
            st.info("Click the link below to sign in with Google, then copy the Verification Code.")
            st.markdown(f"[**👉 Click here to Sign In**]({auth_url})")
            
            auth_code = st.text_input("Paste Verification Code here:", type="password")
            
            if auth_code:
                try:
                    flow.fetch_token(code=auth_code)
                    creds = flow.credentials
                    st.session_state.google_creds = creds
                    st.success("Successfully Logged In!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Authentication failed: {e}")
            
        except Exception as e:
            st.error(f"Flow Error: {e}")
            if st.button("Reset Configuration"):
                st.session_state.oauth_secrets = None
                st.rerun()

    return st.session_state.google_creds
