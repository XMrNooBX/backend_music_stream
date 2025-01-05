import re
import urllib.parse
import isodate
import yt_dlp
from langchain.prompts import ChatPromptTemplate
from langchain_mistralai import ChatMistralAI
from langchain.schema.output_parser import StrOutputParser
from urllib.parse import urlencode
from flask import Flask, jsonify, request
from flask_cors import CORS
import httpx
import asyncio

app = Flask(__name__)
CORS(app)

llmx = ChatMistralAI(
    model="mistral-large-latest",
    temperature=0.2,
    api_key="r1u9jBlZye7QrH3ymxkJjAMVd4VLoSEA",
)

def clean_title(title: str) -> str:
    title = re.sub(r'\[.*?\]|\(.*?\)', '', title)
    title = re.sub(r'[^\w\s]', '', title)
    return re.sub(r'\s+', ' ', title).strip()

async def get_playlist(link):
    async with httpx.AsyncClient() as client:
        base_url = 'https://api.fabdl.com/spotify/get'
        params = {'url': link}
        encoded_params = urlencode(params)
        url = f"{base_url}?{encoded_params}"
        resp = await client.get(url)
        data = resp.json()

        tracks = data['result']['tracks']
        playlist_id = data['result']['gid']
        songs = {}
        for i in tracks:
            cleaned_title = clean_title(i["name"])
            songs[cleaned_title + " " + i["artists"]] = [i['id'], i['image']]
        return songs, playlist_id

async def get_song_url_fabdl(playlist_id, song_id):
    async with httpx.AsyncClient() as client:
        mid_url = f'https://api.fabdl.com/spotify/mp3-convert-task/{playlist_id}/{song_id}'
        resp = await client.get(mid_url)
        response = resp.json()
        dl_id = response['result']['tid']
        streaming_url = f'https://api.fabdl.com/spotify/download-mp3/{dl_id}'
        head_resp = await client.get(streaming_url)
        if head_resp.status_code == 200:
            return streaming_url
        else:
            return None

async def get_song_url_jiosaavn(query):
    async with httpx.AsyncClient() as client:
        results = {}
        url = f"https://www.jiosaavn.com/api.php?__call=autocomplete.get&query={query}&_format=json&_marker=0&ctx=wap6dot0"
        info = await client.get(url)
        if info.status_code == 200:
            resp = info.json().get("songs", {}).get("data", [])
            for i in resp:
                results[f"{i['title']} - {i['description']}"] = i['url']
        try:
            link = results[closest_title_jio(query, list(results.keys()))]
            song_id = re.findall(r'song/(.*?)/(.*)', link)[0]
            url = f'https://www.jiosaavn.com/api.php?__call=webapi.get&api_version=4&_format=json&_marker=0&ctx=wap6dot0&token={song_id[1]}&type=song'
            resp = await client.get(url)
            response = resp.json()
            final_url = urllib.parse.quote(response['songs'][0]['more_info']['encrypted_media_url'])
            dwn_url = f'https://www.jiosaavn.com/api.php?__call=song.generateAuthToken&url={final_url}&bitrate=320&api_version=4&_format=json&ctx=wap6dot0'
            dwn_r = await client.get(dwn_url)
            direct_url = re.findall(r"(.+?(?=Expires))", dwn_r.json()['auth_url'])[0].replace('.cf.', '.').replace('?', '').replace('ac', 'aac')
            direct_resp = await client.get(direct_url)
            if direct_resp.status_code == 200:
                return direct_url
            else:
                return None
        except:
            return None
            
async def get_song_url_youtube(query):
    async with httpx.AsyncClient() as client:
        query = query.replace(' ', '+')
        search_url = f'https://www.youtube.com/results?search_query={query}'

        response = await client.get(search_url)
        video_ids = re.findall(r'videoId\":\"(.*?)\"', response.text)

        if len(video_ids) > 0:
            v_ids = list(dict.fromkeys(video_ids))[:7]

            api_key = "AIzaSyCDE6NS0-Ja-RaIJmsMnm-CP_zHThAth8A"
            api_url = f"https://www.googleapis.com/youtube/v3/videos?part=snippet,contentDetails&id={','.join(v_ids)}&key={api_key}"

            api_response = await client.get(api_url)
            video_dict = {}
            if "items" in api_response.json():
                for video in api_response.json()["items"]:
                    title = clean_title(video["snippet"]["title"])
                    duration = video["contentDetails"]["duration"]
                    video_id = video["id"]
                    duration_seconds = isodate.parse_duration(duration).total_seconds()
                    if duration_seconds > 60:
                        video_dict[title] = video_id

            yt_id = video_dict[closest_title(query, list(video_dict.keys()))]
            URL = f'https://www.youtube.com/watch?v={yt_id}'
            ydl_opts = {}
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(URL, download=False)
                    sanitized_info = ydl.sanitize_info(info)['formats']
                    for i in sanitized_info:
                        if i['resolution'] == "audio only" and "audio_channels" in i:
                            return i['url']
            except:
                return None

# Function to get the streaming URL
async def get_song_url(playlist_id, song_id, query):
    try:
        fabdl_url = await get_song_url_fabdl(playlist_id, song_id)
        if fabdl_url:
            return fabdl_url
        else:
            raise ValueError
    except Exception:
        jiosaavn_url = await get_song_url_jiosaavn(query)
        if jiosaavn_url:
            return jiosaavn_url
        else:
            youtube_url = await get_song_url_youtube(query)
            return youtube_url

def closest_title_jio(query, titles, llm=llmx):
    # The LLM calls are synchronous, so we don't need to change this function
    system = f"""
    Given a user search query song name: "{query}" and a list of song titles from JioSaavn:
    {titles},
    return ONLY one complete and correct song title from the list that most closely matches the user's intent.

    Important Instructions:
    - **The returned title MUST be exactly as it appears in the provided list. Do not modify the title in any way.**
    - Prioritize exact or near-exact matches to the query.
    - If the query matches part of a song title exactly (e.g., words or phrases), return the full title of that match, even if other titles are longer or contain additional details (like artist names).
    - If multiple titles are very similar to the query, choose the one that is the closest match in terms of wording and overall meaning.
    - If no potential match is found among the provided titles, return "False".
    - **Do not add any explanation or extra content. Only return the exact title of the best match from the list or "False".**
    """
    user = f"{query}"

    filtering_prompt = ChatPromptTemplate.from_messages(
        [
            ("system", system),
            ("user", user)
        ]
    )

    filtering_chain = filtering_prompt | llm | StrOutputParser()

    response = filtering_chain.invoke({"query": query})

    return response if response else False


def closest_title(query, titles, llm=llmx):
    system = f"""
    You are tasked with analyzing a list of YouTube video titles to find the most relevant match for a given song query.

    The query: "{query}"
    The list of YouTube titles: {titles}

    Your goal:
    - **Return ONLY one of the titles provided in the list.** Do NOT modify the title. Do NOT add or remove any words.
    - Always prioritize the closest match to the query.
    - If an exact or near-exact match for the query is found, return that title, even if other versions like mixes or remixes are present.
    - If there are multiple plausible matches, choose the one that is closest to the query in wording and intent.
    - If none of the titles plausibly match the query, return the first title from the list.
    - **Do not add any explanations, reasoning, or extra content. Only return the title as your output.**
    """

    user = f"The query is: {query}"

    filtering_prompt = ChatPromptTemplate.from_messages(
        [
            ("system", system),
            ("user", user)
        ]
    )

    filtering_chain = filtering_prompt | llm | StrOutputParser()

    try:
        response = filtering_chain.invoke({"query": query})
        if response and response.strip():
            result = response.strip()
            if result in titles:
                return result
            else:
                print(f"Warning: Title '{result}' not found in provided titles.")
    except Exception as e:
        print(f"Error in closest_title LLM processing: {e}")

    return titles[0]  # Default to the first title if no match is found



async def get_jiosaavn_download_link(link, query):
    async with httpx.AsyncClient() as client:
        song_id = re.findall(r'song/(.*?)/(.*)', link)[0]
        url = f'https://www.jiosaavn.com/api.php?__call=webapi.get&api_version=4&_format=json&_marker=0&ctx=wap6dot0&token={song_id[1]}&type=song'
        resp = await client.get(url)
        response = resp.json()
        final_url = urllib.parse.quote(response['songs'][0]['more_info']['encrypted_media_url'])
        dwn_url = f'https://www.jiosaavn.com/api.php?__call=song.generateAuthToken&url={final_url}&bitrate=320&api_version=4&_format=json&ctx=wap6dot0'
        dwn_r = await client.get(dwn_url)
        direct_url = re.findall(r"(.+?(?=Expires))", dwn_r.json()['auth_url'])[0].replace('.cf.', '.').replace('?', '').replace('ac', 'aac')
        direct_resp = await client.get(direct_url)
        if direct_resp.status_code == 200:
            return direct_url
        else:
            return await get_song_url_youtube(query)

async def get_youtube_download_link(video_id):
    async with httpx.AsyncClient() as client:
        return await get_song_url_youtube(f"https://www.youtube.com/watch?v={video_id}")

async def search_jiosaavn(query):
    async with httpx.AsyncClient() as client:
        results = {}
        url = f"https://www.jiosaavn.com/api.php?__call=autocomplete.get&query={query}&_format=json&_marker=0&ctx=wap6dot0"
        try:
            info = await client.get(url)
            if info.status_code == 200:
                resp = info.json().get("songs", {}).get("data", [])
                for i in resp:
                    title = f"{i['title']} - {i['description']}"
                    results[title] = [i['url'], i['image']]
        except Exception as e:
            print(f"Error searching JioSaavn: {e}")
        return results

async def search_youtube(query):
    async with httpx.AsyncClient() as client:
        results = {}
        search_url = f'https://www.youtube.com/results?search_query={query.replace(" ", "+")}'
        try:
            response = await client.get(search_url)
            video_ids = re.findall(r'videoId\":\"(.*?)\"', response.text)

            if len(video_ids) > 0:
                # Get unique video IDs and limit to 7
                v_ids = list(dict.fromkeys(video_ids))[:7]

                # Fetch video details from YouTube Data API
                api_key = "AIzaSyCDE6NS0-Ja-RaIJmsMnm-CP_zHThAth8A" # Replace with your YouTube Data API key
                api_url = f"https://www.googleapis.com/youtube/v3/videos?part=snippet,contentDetails&id={','.join(v_ids)}&key={api_key}"
                api_response = await client.get(api_url)
                api_response_json = api_response.json()

                if "items" in api_response_json:
                    for video in api_response_json["items"]:
                        # Extract video details
                        title = video["snippet"]["title"]
                        duration = video["contentDetails"]["duration"]
                        video_id = video["id"]
                        thumbnails = video["snippet"]["thumbnails"]
                        thumbnail_url = thumbnails["high"]["url"] if "high" in thumbnails else thumbnails["default"]["url"]

                        # Convert duration to seconds
                        duration_seconds = isodate.parse_duration(duration).total_seconds()

                        # Add to results if duration is greater than 60 seconds
                        if duration_seconds > 60:
                            results[title] = [video_id, thumbnail_url]
        except Exception as e:
            print(f"Error searching YouTube: {e}")
        return results

async def get_search(query: str):
    jiosaavn_results, youtube_results = await asyncio.gather(
        search_jiosaavn(query),
        search_youtube(query)
    )
    results = {**jiosaavn_results, **youtube_results}
    return results

@app.route('/get_search', methods=['POST'])
async def api_get_search():
    data = request.get_json()
    query = data.get('query')
    
    if not query:
        return jsonify({'error': 'Missing query'}), 400

    songs = await get_search(query)
    return jsonify({'songs': songs})

@app.route('/get_playlist', methods=['POST'])
async def api_get_playlist():
    data = request.get_json()
    link = data.get('link')
    
    if not link:
        return jsonify({'error': 'Missing playlist link'}), 400

    songs, playlist_id = await get_playlist(link)
    return jsonify({'playlist_id': playlist_id, 'songs': songs})

@app.route('/get_song', methods=['POST'])
async def api_get_song():
    data = request.get_json()
    
    playlist_id = data.get('playlist_id')
    song_id = data.get('song_id')
    query = data.get('query')

    if not playlist_id and 'jiosaavn' in song_id and query:
        song_url = await get_jiosaavn_download_link(song_id, query)
    elif not playlist_id and 'jiosaavn' not in song_id and query:
        song_url = await get_youtube_download_link(song_id)
    elif not playlist_id or not song_id or not query:
        return jsonify({'error': 'Missing parameters'}), 400
    else:
        song_url = await get_song_url(playlist_id, song_id, query)

    if song_url:
        return jsonify({'song_url': song_url})
    else:
        return jsonify({'error': 'Could not retrieve song URL'}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0')
