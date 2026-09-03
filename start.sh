export FNS_API=http://10.99.0.31:9000
export FNS_TOKEN=$(cat ~/.config/sdlc-board/token)
export FNS_VAULT=sdlc
export FNS_CLIENT=sdlcBoard          # must match the token's client restriction
 
python3 server.py 8787
