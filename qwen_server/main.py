import datetime
import importlib
import multiprocessing
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import jsonlines
import requests
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import config_browserqwen

sys.path.insert(
    0,
    str(Path(__file__).absolute().parent.parent))  # NOQA

from qwen_agent.actions import Simple  # NOQA
from qwen_agent.tools.parse_doc import parse_pdf_pypdf, parse_html_bs  # NOQA
from qwen_agent.utils.util import print_traceback, save_text_to_file  # NOQA
from qwen_agent.schema import Record  # NOQA

prompt_lan = sys.argv[1]
llm_name = sys.argv[2]
max_ref_token = int(sys.argv[3])
workstation_port = int(sys.argv[4])
model_server = sys.argv[5]
api_key = sys.argv[6]
server_host = sys.argv[7]

app = FastAPI()

origins = [
    'http://127.0.0.1:'+str(workstation_port),
    'http://localhost:'+str(workstation_port),
    'http://0.0.0.0:'+str(workstation_port),
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

app.mount('/static', StaticFiles(directory=config_browserqwen.code_interpreter_ws), name='static')

if llm_name.startswith('gpt'):
    module = 'qwen_agent.llm.gpt'
    llm = importlib.import_module(module).GPT(llm_name)
elif llm_name.startswith('Qwen') or llm_name.startswith('qwen'):
    module = 'qwen_agent.llm.qwen'
    llm = importlib.import_module(module).Qwen(llm_name, model_server=model_server, api_key=api_key)
else:
    raise NotImplementedError


def update_pop_url(data, cache_file_popup_url):
    new_line = {'url': data['url']}

    with jsonlines.open(cache_file_popup_url, mode='w') as writer:
        writer.write(new_line)

    response = 'Update URL'
    return response


def is_local_path(path):
    if path.startswith('file://'):
        return True
    else:
        return False


def get_title(text, cacheprompt=''):
    agent = Simple(llm=llm, stream=False)
    extract = agent.run(text, cacheprompt)

    return extract


def download_pdf(url, save_path):
    response = requests.get(url)
    with open(save_path, 'wb') as file:
        file.write(response.content)


def sanitize_chrome_file_path(file_path: str) -> str:
    # For Linux and macOS.
    if os.path.exists(file_path):
        return file_path

    # For native Windows, drop the leading '/' in '/C:/'
    win_path = file_path
    if win_path.startswith('/'):
        win_path = win_path[1:]
    if os.path.exists(win_path):
        return win_path

    # For Windows + WSL.
    if re.match(r'^[A-Za-z]:/', win_path):
        wsl_path = f'/mnt/{win_path[0].lower()}/{win_path[3:]}'
        if os.path.exists(wsl_path):
            return wsl_path

    # For native Windows, replace / with \.
    win_path = win_path.replace('/', '\\')
    if os.path.exists(win_path):
        return win_path

    return file_path


def cache_data(data, cache_file):
    extract = ''  # extract a title for display
    print('Begin cache...')
    if data['url'][-4:] in ['.pdf', '.PDF']:
        date1 = datetime.datetime.now()

        # generate one processing record
        new_record = Record(url=data['url'], time='', type=data['type'], raw=[],
                            extract='', topic='', checked=False, session=[]).to_dict()
        with jsonlines.open(cache_file, mode='a') as writer:
            writer.write(new_record)

        # deal pdf path
        if is_local_path(data['url']):
            parsed_url = urlparse(data['url'])
            print('parsed_url: ', parsed_url)
            pdf_path = unquote(parsed_url.path)
            pdf_path = sanitize_chrome_file_path(pdf_path)
        else:
            pdf_path = data['url']

        try:
            pdf_content = parse_pdf_pypdf(pdf_path, pre_gen_question=config_browserqwen.pre_gen_question)
        except Exception:
            print_traceback()
            # del the processing record
            lines = []
            if os.path.exists(cache_file):
                for line in jsonlines.open(cache_file):
                    if line['url'] != data['url']:
                        lines.append(line)
            with jsonlines.open(cache_file, mode='w') as writer:
                for new_line in lines:
                    writer.write(new_line)
            return 'failed'

        date2 = datetime.datetime.now()
        print('parse pdf time: ', date2 - date1)
        data['content'] = pdf_content
        data['type'] = 'pdf'
        if prompt_lan == 'CN':
            cacheprompt = '参考资料是一篇论文的首页，请提取出一句话作为标题。'
        elif prompt_lan == 'EN':
            cacheprompt = 'The reference material is the first page of a paper. Please extract one sentence as the title'
        extract = get_title(pdf_content[0]['page_content'], cacheprompt=cacheprompt)
    else:
        if data['content'] and data['type'] == 'html':
            new_record = Record(url=data['url'], time='', type=data['type'], raw=[], extract='', topic='', checked=False, session=[]).to_dict()
            with jsonlines.open(cache_file, mode='a') as writer:
                writer.write(new_record)

            try:
                tmp_html_file = os.path.join(config_browserqwen.cache_root, 'tmp.html')
                save_text_to_file(tmp_html_file, data['content'])
                data['content'] = parse_html_bs(tmp_html_file, pre_gen_question=config_browserqwen.pre_gen_question)
            except Exception:
                print_traceback()
            extract = data['content'][0]['metadata']['title']

    today = datetime.date.today()
    new_record = Record(url=data['url'], time=str(today), type=data['type'], raw=data['content'],
                        extract=extract, topic='', checked=True, session=[])
    lines = []
    if os.path.exists(cache_file):
        for line in jsonlines.open(cache_file):
            if line['url'] != data['url']:
                lines.append(line)
    lines.append(new_record.to_dict())  # cache
    with jsonlines.open(cache_file, mode='w') as writer:
        for new_line in lines:
            writer.write(new_line)

    response = 'Cached'
    return response


def change_checkbox_state(text, cache_file):
    if not os.path.exists(cache_file):
        return {'result': 'no file'}
    lines = []
    for line in jsonlines.open(cache_file):
        if line['url'] == text[3:]:
            if line['checked']:
                line['checked'] = False
            else:
                line['checked'] = True
        lines.append(line)

    with jsonlines.open(cache_file, mode='w') as writer:
        for new_line in lines:
            writer.write(new_line)

    return {'result': 'changed'}


def update_addr_for_figure(address):
    new_line = {'address': address}

    with jsonlines.open(config_browserqwen.address_file, mode='w') as writer:
        writer.write(new_line)

    response = 'Update Address'
    print('Update Address')
    return response


@app.post('/endpoint')
async def web_listening(request: Request):
    data = await request.json()
    msg_type = data['task']

    cache_file_popup_url = os.path.join(config_browserqwen.cache_root, config_browserqwen.url_file)
    cache_file = os.path.join(config_browserqwen.cache_root, config_browserqwen.browser_cache_file)

    if msg_type == 'change_checkbox':
        rsp = change_checkbox_state(data['ckid'], cache_file)
    elif msg_type == 'cache':
        cache_obj = multiprocessing.Process(target=cache_data, args=(data, cache_file))
        cache_obj.start()
        # rsp = cache_data(data, cache_file)
        rsp = 'caching'
    elif msg_type == 'pop_url':
        # What a misleading name! pop_url actually means add_url. pop is referring to the pop_up ui.
        rsp = update_pop_url(data, cache_file_popup_url)
    elif msg_type == 'set_addr':
        rsp = update_addr_for_figure(data['addr'])

    return JSONResponse(content=rsp)


if __name__ == '__main__':
    uvicorn.run(app='main:app', host=server_host, port=config_browserqwen.fast_api_port, reload=True)
