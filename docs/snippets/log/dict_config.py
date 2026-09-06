import uvicorn
from fastapi import FastAPI

from grelmicro.log import dict_config

app = FastAPI()

if __name__ == "__main__":
    uvicorn.run(app, log_config=dict_config())
