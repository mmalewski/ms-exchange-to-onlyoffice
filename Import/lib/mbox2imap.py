#!/usr/bin/env python2.7
#-*- encoding: utf-8 -*-
 
import os, sys, time
import re
import ssl
import progressbar
from time import sleep
 
from argparse import ArgumentParser
from getpass import getpass
 
import imaplib
from email.header import decode_header
from mailbox import mbox
 
from twisted.mail import imap4 # for their imap4-utf7 implementation
 
parser = ArgumentParser(description="""Recursively import mbox files in a directory
                                        structure to an IMAP server.\n
                                        The expected structure is that generated by
                                        'readpst -r'.""")
parser.add_argument('-s', dest='imap_server', default='localhost', help='IMAP server to import emails to')
parser.add_argument('-u', dest='imap_user', required=True, help='user for logging in to IMAP')
parser.add_argument('-p', dest='imap_passwd', help="will be prompted for if not provided")
parser.add_argument('-c', dest='charset', default='utf8', help='charset in which the folders are stored (for versions older than 2003)')
parser.add_argument('-f', dest='force', action='store_true', help='import mail even if we think it might be a duplicate')
parser.add_argument('-m', dest='mappings', help='a JSON file with mappings between folder names and mailbox names (no slashes or dots)')
parser.add_argument('folder', nargs='+', help="the base folders to import")
 
args = parser.parse_args()
 
if not args.imap_passwd:
    args.imap_passwd = getpass()
 
if args.mappings:
    import json
    folderToMailbox = json.load(open(args.mappings,'r'))
else:
    folderToMailbox = {}
 
def mailboxFromPath(path):
    paths = []
    for p in path.split(os.path.sep):
        p = folderToMailbox.get(p, p) # get value or default
 
        # only other invalid char besides '/', which can't be created by readpst anyway
        p = p.replace('.','')
        paths.append(p)
 
    r = '/'.join(paths)
    print "mailboxFromPath '" + path + "' changed to '" + r + "'"
    return r
 
def imapFlagsFromMbox(flags):
    # libpst only sets R and O
    f = []
    if 'R' in flags or 'O' in flags:
        f.append(r'\Seen')
    if 'D' in flags:
        f.append(r'\Deleted')
    if 'A' in flags:
        f.append(r'\Answered')
    if 'F' in flags:
        f.append(r'\Flagged')
    return '('+' '.join(f)+')'
 
def utf7encode(s):
    return imap4.encoder(s)[0]
 
def headerToUnicode(s):
    h = decode_header(s)[0]
    try:
        if h[1]: # charset != None
            try:
                return unicode(*h)
            except LookupError:
                return unicode(h[0],'utf8','replace')
        else:
            return unicode(h[0], 'utf8')
    except UnicodeDecodeError:
        try:
            return unicode(h[0], 'cp1252') # the usual culprits for malformed headers
        except UnicodeDecodeError:
            pass
 
        try:
            return unicode(h[0], 'latin1') # the usual culprits for malformed headers
        except UnicodeDecodeError:
            pass
 
        return unicode(h[0], 'ascii', 'ignore') # give up...

Commands = {
  'STARTTLS': ('NONAUTH')
}

imaplib.Commands.update(Commands)

class IMAP4_STARTTLS(imaplib.IMAP4, object):
  def __init__(self, host, port):
    super(IMAP4_STARTTLS, self).__init__(host, port)
    self.__starttls__()
    self.__capability__()

  def __starttls__(self, keyfile = None, certfile = None):
    typ, data = self._simple_command('STARTTLS')
    if typ != 'OK':
      raise self.error('no STARTTLS')
    self.sock = ssl.wrap_socket(self.sock,
      keyfile,
      certfile,
      ssl_version=ssl.PROTOCOL_TLSv1)
    self.file.close()
    self.file = self.sock.makefile('rb')

  def __capability__(self):
    typ, dat = super(IMAP4_STARTTLS, self).capability()
    if dat == [None]:
      raise self.error('no CAPABILITY response from server')
    self.capabilities = tuple(dat[-1].upper().split())

def main():
    imap = IMAP4_STARTTLS(args.imap_server, 143)
    imap.login(args.imap_user, args.imap_passwd)
 
    imap.select()
    for base in args.folder:
        print "importing folder "+base
        for root, dirs, files in os.walk(base):
            if 'mbox' in files:
                folder = unicode(os.path.relpath(root, base), args.charset)
                mailbox = mailboxFromPath(folder)
                print u'importing mbox in {0} to {1}'.format(folder, mailbox)
                mailbox_encoded = utf7encode(mailbox)

                r = imap.select(mailbox_encoded)
                print r

                if r[0] != 'OK':
                    if '[CANNOT]' in str(r[1]):
                        sys.stderr.write("Could not select mailbox: " + str(r))
                        continue

                    print "creating mailbox " + mailbox
                    r = imap.create(mailbox_encoded)
                    if r[0] != 'OK':
                        sys.stderr.write("Could not create mailbox: " + str(r))
                        continue

                    r = imap.subscribe(mailbox_encoded)
                    print r
                    r = imap.select(mailbox_encoded)
                    print r

                m = mbox(os.path.join(root, 'mbox'), create=False)

                total = len(m)
                print "Found {0} messages".format(total)

                widgets = [progressbar.Bar('=', '[', ']'), ' ', progressbar.SimpleProgress()]
                bar = progressbar.ProgressBar(maxval=total, widgets=widgets)
                bar.start()

                i = 0
                skipped = 0
                failed = 0

                for msg in m:
                    i += 1
                    bar.update(i)
                    sleep(0.01) 

                    # skip possibly duplicated msgs
                    query = 'FROM "{0}" SUBJECT "{1}"'.format(
                                utf7encode(headerToUnicode(msg['from']).replace('"','')),
                                utf7encode(headerToUnicode(msg['subject']).replace('"',r'\"'))
                            )
                    if msg.has_key('date'):
                        query += ' HEADER DATE "{0}"'.format(utf7encode(msg['date']))
                    if msg.has_key('message-id') and msg['message-id']:
                        query += ' HEADER MESSAGE-ID "{0}"'.format(utf7encode(msg['message-id']))

                    try:
                        r = imap.search(None, '({0})'.format(query))
                        if r[1][0] and not args.force:
                            # print "skipping "+mailbox+": '"+headerToUnicode(msg['subject'])[:20]+"' (mid: "+str(msg['message-id'])+")"
                            skipped+=1
                            continue
    
                        r = imap.append(mailbox_encoded, '', imaplib.Time2Internaldate(time.time()), str(msg))
                        if r[0] != 'OK':
                            failed += 1
                            # sys.stderr.write("failed to import {0} ({1}): {2}".format(msg['message-id'], msg['date'], r[1]))
                            continue
                        num = re.sub(r'.*APPENDUID \d+ (\d+).*', r'\1', r[1][0])
                        r = imap.uid('STORE', str(num), "FLAGS", imapFlagsFromMbox(msg.get_flags()))

                    except:
                        print "Unexpected error:", sys.exc_info()[0]
                        break;

                    #if r[0] != 'OK':
                    #    sys.stderr.write("failed to set flags for msg {0} in {1}".format(num, mailbox))

                bar.finish()
                print("Skipped {0} | failed {1} | total count {2}\n".format(skipped, failed, total))
 
    imap.logout()
 
if __name__ == '__main__':
    main()