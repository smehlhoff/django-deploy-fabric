import json
import os
import secrets

import boto3
from fabric import Connection, task
from jinja2 import Environment, FileSystemLoader

config = json.loads(open('config.json').read())
client = boto3.client('ssm', region_name=config['prod']['region'])


def get_connection(ctx):
    try:
        return Connection(ctx.host, ctx.user, connect_kwargs=ctx.connect_kwargs)
    except:
        return None


def get_store_parameter(key):
    resp = client.get_parameter(Name=key, WithDecryption=True)
    return resp['Parameter']['Value']


def create_store_parameters():
    client.put_parameter(Name='/prod/PSQL_USER',
                         Value=secrets.token_urlsafe(8), Type='String')
    client.put_parameter(Name='/prod/PSQL_PASSWORD',
                         Value=secrets.token_urlsafe(64), Type='String')
    client.put_parameter(Name='/prod/PSQL_DB',
                         Value=secrets.token_urlsafe(8), Type='String')


def create_config_files():
    env = Environment(loader=FileSystemLoader('templates'))

    gunicorn_service_template = env.get_template('gunicorn.service.j2')
    gunicorn_service_output = gunicorn_service_template.render(
        project=config['project']['name'])

    with open('gunicorn.service', 'w') as f:
        f.write(gunicorn_service_output)

    gunicorn_socket_template = env.get_template('gunicorn.socket.j2')
    gunicorn_socket_output = gunicorn_socket_template.render()

    with open('gunicorn.socket', 'w') as f:
        f.write(gunicorn_socket_output)

    nginx_template = env.get_template('nginx.j2')
    nginx_output = nginx_template.render(
        host=config['prod']['host'], project=config['project']['name'])

    with open('nginx.conf', 'w') as f:
        f.write(nginx_output)


@task
def prod(ctx):
    ctx.user = config['prod']['user']
    ctx.host = config['prod']['host']
    ctx.connect_kwargs.key_filename = config['prod']['key']


@task
def setup(ctx):
    create_store_parameters()

    psql_user = get_store_parameter('/prod/PSQL_USER')
    psql_password = get_store_parameter('/prod/PSQL_PASSWORD')
    psql_db = get_store_parameter('/prod/PSQL_DB')

    c = get_connection(ctx)
    c.sudo('apt-get update')
    c.sudo('DEBIAN_FRONTEND=noninteractive apt-get upgrade -y')
    c.sudo('apt-get install python3-pip python3-dev libpq-dev postgresql postgresql-contrib nginx supervisor -y')
    c.sudo('-H pip3 install virtualenv')

    c.sudo('-u postgres psql -c "CREATE DATABASE {}"'.format(psql_db))
    c.sudo('-u postgres psql -c "CREATE USER {} WITH PASSWORD\'{}\'"'.format(psql_user, psql_password))
    c.sudo('-u postgres psql -c "ALTER ROLE {} SET client_encoding TO \'utf8\'"'.format(psql_user))
    c.sudo('-u postgres psql -c '
           '"ALTER ROLE {} SET default_transaction_isolation TO \'read committed\'"'.format(psql_user))
    c.sudo('-u postgres psql -c "ALTER ROLE {} SET timezone TO \'UTC\'"'.format(psql_user))
    c.sudo('-u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE {} TO {}"'.format(psql_db, psql_user))

    c.sudo('chmod 755 /home/ubuntu')
    c.run('git clone {}'.format(config['project']['gitlab']))
    c.run('virtualenv /home/ubuntu/{}/env'.format(config['project']['name']))
    c.run('cd /home/ubuntu/{} && source env/bin/activate && '
          'pip install -r requirements/prod.txt && '
          'python manage.py makemigrations && '
          'python manage.py migrate'.format(config['project']['name']))
    c.run('cd /home/ubuntu')

    create_config_files()

    c.put('gunicorn.socket', '/home/ubuntu')
    c.sudo('mv /home/ubuntu/gunicorn.socket /etc/systemd/system/')
    c.put('gunicorn.service', '/home/ubuntu')
    c.sudo('mv /home/ubuntu/gunicorn.service /etc/systemd/system/')
    c.sudo('chmod 644 /etc/systemd/system/gunicorn.socket')
    c.sudo('chmod 644 /etc/systemd/system/gunicorn.service')
    c.sudo('systemctl start gunicorn.socket')
    c.sudo('systemctl enable gunicorn.socket')

    c.put('nginx.conf', '/home/ubuntu')
    c.sudo(
        'mv /home/ubuntu/nginx.conf /etc/nginx/sites-available/{}'.format(config['project']['name']))
    c.sudo(
        'chmod 644 /etc/nginx/sites-available/{}'.format(config['project']['name']))
    c.sudo(
        'ln -s /etc/nginx/sites-available/{} /etc/nginx/sites-enabled'.format(config['project']['name']))
    c.sudo('systemctl restart nginx')
    c.close()

    files = ['gunicorn.service', 'gunicorn.socket', 'nginx.conf']

    for file in files:
        os.remove(file)


@task
def update(ctx):
    c = get_connection(ctx)
    c.sudo('apt-get update')
    c.sudo('DEBIAN_FRONTEND=noninteractive apt-get upgrade -y')
    c.sudo('DEBIAN_FRONTEND=noninteractive apt-get dist-upgrade -y')


@task
def deploy(ctx):
    c = get_connection(ctx)
    c.run('cd /home/ubuntu/{} && git pull'.format(config['project']['name']))
    c.run('cd /home/ubuntu/{} && source env/bin/activate && '
          'python manage.py makemigrations && '
          'python manage.py migrate'.format(config['project']['name']))
    c.sudo('systemctl restart gunicorn')
    c.close()
