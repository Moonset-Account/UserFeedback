def notify(email, need_mask):
    if need_mask:
        shown = mask_email(email)
    else:
        shown = email
    send_alert(email)
    logger.info("email=%s", shown)
