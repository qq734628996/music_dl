# -*- coding: utf-8 -*-

import json
import langid
import os
import requests
import time

from concurrent.futures import ThreadPoolExecutor, as_completed
from mutagen import id3
from mutagen.flac import FLAC
from PIL import Image
from tqdm import tqdm

# https://binaryify.github.io/NeteaseCloudMusicApi/#/
API='http://localhost:3000'
API_PLAYLIST=API+'/playlist/detail'
API_SONG=API+'/song/url'
API_LYRIC=API+'/lyric'
LANG={'en':'eng','zh':'chi','ja':'jpn','es':'spa','ru':'rus','de':'ger','fr':'fre','ko':'kor','ro':'rum'}
LANG_DEFAULT='eng'
ID_WIDTH=0

STORE_PATH=''
MAX_POOL=32
BITRATE=320000 # 999000

def get_playlist(playlist_id):
    data=requests.get(API_PLAYLIST,{
        'id':playlist_id,
    }).json()
    return data['playlist']

def get_song_url(song_id):
    time.sleep(0.05)
    song_info=requests.get(API_SONG,{
        'id':song_id,
        'br':BITRATE,
    }).json()
    song_url=song_info['data'][0]['url']
    song_type=song_info['data'][0]['type']
    return song_url,song_type

def get_lyric(song_id):
    '''
    langid can identify language, but it's format is different from ID3.
    '''
    time.sleep(0.05)
    data=requests.get(API_LYRIC,{
        'id':song_id,
    }).json()
    lrc=[]
    if 'lrc' in data and 'lyric' in data['lrc'] and data['lrc']['lyric']:
        lang=langid.classify(data['lrc']['lyric'])[0]
        if lang in LANG:
            lang=LANG[lang]
        else:
            lang=LANG_DEFAULT
        lrc.append({
            'lang': lang,
            'text': data['lrc']['lyric']
        })
    if 'tlyric' in data and 'lyric' in data['tlyric'] and data['tlyric']['lyric']:
        lang=LANG[langid.classify(data['tlyric']['lyric'])[0]]
        lrc.append({
            'lang': lang,
            'text': data['tlyric']['lyric']
        })
    return lrc

def windows_file(file):
    '''
    replace illegal characters of windows' filename.
    '''
    for i in '\\/:*?\"<>|':
        file=file.replace(i,chr(ord(i)+65248))
    return file

def create_dir(data):
    playlist_name='[{0}] - {1}'.format(','.join(data['tags']), data['name'])
    playlist_name=windows_file(playlist_name)
    path=os.path.join(STORE_PATH,playlist_name)
    if not os.path.exists(path):
        os.mkdir(path)
    return path

def write_info_json(path,data):
    with open(os.path.join(path,'info.json'),'w') as f:
        json.dump(data,f)

def get_one_song_info(songs,i,x):
    song_url,song_type=get_song_url(x['id'])
    lrc=get_lyric(x['id'])
    if song_url is None:
        return False
    songs.append((i,{
        'lrc': lrc,
        'type': song_type,
        'id': x['id'],
        'title': x['name'],
        'artist': [y['name'] for y in x['ar']],
        'album': x['al']['name'],
        'pic_url': x['al']['picUrl'],
        'disc': x['cd'],
        'track': x['no'],
    }))
    time.sleep(0.05)
    return True

def get_songs_info(path,data):
    path_json=os.path.join(path,'mini_info.json')
    if os.path.exists(path_json):
        with open(path_json) as f:
            return json.load(f)
    songs=[]
    print('Geting songs information...')
    tqdm.get_lock()
    with tqdm(total=len(data['tracks']),ncols=70) as pbar:
        pool=ThreadPoolExecutor(max_workers=MAX_POOL)
        tasks=[pool.submit(get_one_song_info,songs,i,x) for i,x in enumerate(data['tracks'])]
        for state in as_completed(tasks):
            if state._result:
                pbar.update()
        songs=sorted(songs,key=lambda x: x[0])
        songs=[x[1] for x in songs]
    with open(path_json,'w') as f:
        json.dump(songs,f)
    return songs

def download_file(url,path):
    r=requests.get(url,stream=True)
    with open(path,'wb') as f:
        for chunk in r.iter_content(chunk_size=128):
            f.write(chunk)

def download_pic(path,url):
    '''
    download and make a jpeg thumbnail.
    '''
    path_pic=None
    if url:
        path_pic=os.path.splitext(path)[0]+'.jpg'
        try:
            download_file(url,path_pic)
            with Image.open(path_pic) as im:
                im.thumbnail((640,640))
                im.convert('RGB').save(path_pic)
        except:
            path_pic=None
    return path_pic

def tag_mp3(path_audio,path_pic,lrc,title,artist,album,disc,track,**kw):
    '''
    ref:
    http://id3.org/id3v2.3.0
    https://github.com/quodlibet/mutagen/blob/master/mutagen/id3/_frames.py
    http://help.mp3tag.de/main_tags.html
    http://code.activestate.com/recipes/577138-embed-lyrics-into-mp3-files-using-mutagen-uslt-tag/
    '''
    tags=id3.ID3()
    if path_pic:
        with open(path_pic,'rb') as f:
            tags['APIC']=id3.APIC(mime='image/jpeg',type=id3.PictureType.COVER_FRONT,data=f.read())
    for x in lrc:
        tags["USLT::"+x['lang']]=id3.USLT(text=x['text'],lang=x['lang'])
    tags['TIT2']=id3.TIT2(text=title)
    tags['TPE1']=id3.TPE1(text=artist)
    if album!=None:
        tags['TALB']=id3.TALB(text=album)
    tags['TPOS']=id3.TPOS(text=disc)
    tags['TRCK']=id3.TRCK(text=str(track))
    tags.save(path_audio)

def tag_flac(path_audio,path_pic,lrc,title,artist,album,disc,track,**kw):
    '''
    ref:
    https://www.xiph.org/vorbis/doc/v-comment.html
    FLAC tags also call Vorbis comment.
    It doesn't support lyrics.
    So use ID3 to tag FLAC instead FLAC tags.
    '''
    tags=FLAC(path_audio)
    tags.clear()
    tags.clear_pictures()
    tags.save()
    tag_mp3(path_audio,path_pic,lrc,title,artist,album,disc,track,**kw)

def download_song(path,cnt,song):
    file_name='{0:0{1}}.{2}.{3}'.format(cnt,ID_WIDTH,song['title'],song['type'])
    path_song=os.path.join(path,windows_file(file_name))
    if not os.path.exists(path_song):
        path_song_part=path_song+'.part'
        song_url,song_type=get_song_url(song['id'])
        download_file(song_url,path_song_part)
        path_pic=download_pic(path_song,song['pic_url'])
        if song_type == 'flac':
            tag_flac(path_song_part,path_pic,**song)
        elif song_type == 'mp3':
            tag_mp3(path_song_part,path_pic,**song)
        os.remove(path_pic)
        os.rename(path_song_part,path_song)
        time.sleep(0.05)

def playlist_dl(playlist_id):
    data=get_playlist(playlist_id)
    print('Start Dowdloading {0}.'.format(data['name']))
    path=create_dir(data)
    write_info_json(path,data)
    songs=get_songs_info(path,data)
    global ID_WIDTH
    ID_WIDTH=len(str(len(songs)))
    print('Dowdloading songs...')
    tqdm.get_lock()
    with tqdm(total=len(songs),ncols=70) as pbar:
        pool=ThreadPoolExecutor(max_workers=MAX_POOL)
        tasks=[pool.submit(download_song,path,i+1,song) for i,song in enumerate(songs)]
        for _ in as_completed(tasks):
            pbar.update()

playlists=[
626745111,
65321097,
815573174,
498708023,
39477061,
111221399,
]

def main():
    for i in playlists:
        playlist_dl(i)

if __name__ == "__main__":
    main()
